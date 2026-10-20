"""Luna's full-screen Textual TUI application."""

from __future__ import annotations

from luna_core.client import LunaClient, ServerError
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer

from luna.tui.chat import ChatPane
from luna.tui.errors import readable
from luna.tui.sidebar_sessions import SessionsSidebar
from luna.tui.theme import TUI_VARIABLES


class LunaApp(App):
    """The full-screen Luna TUI — a thin client over the local server."""

    # Textual's own global command palette (Ctrl+P by default, listing
    # every registered System Command) is a second, unrelated "type things
    # to find a command" surface that has nothing to do with the chat
    # input's own "/"-prefixed slash-command dropdown (ChatPane's
    # #autocomplete) — having both confused more than it helped. The
    # slash dropdown is the TUI's one command search, built into the input
    # line itself.
    ENABLE_COMMAND_PALETTE = False

    # One external stylesheet for the whole app (Textual's own recommended
    # practice for an app's own widgets — see luna.tcss's header comment),
    # not a DEFAULT_CSS string duplicated across every widget's .py file.
    CSS_PATH = "luna.tcss"

    BINDINGS = [
        ("ctrl+b", "toggle_panels", "Панель"),
        ("ctrl+n", "new_session", "Новая сессия"),
        ("ctrl+g", "send_background", "В фон"),
    ]

    #: Run/task statuses that are still in flight.
    _LIVE = ("queued", "running", "waiting")
    _VERBS = {"done": "завершена", "failed": "ошибка", "cancelled": "отменена"}

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        workdir: str,
        thread_id: str,
        provider: str = "",
        model: str | None = None,
        pricing: dict | None = None,
    ) -> None:
        super().__init__()
        self._workdir = workdir
        self._provider = provider
        self._model = model
        self._pricing = pricing
        # The CLI already resolved the thread to start on (a fresh uuid4 hex,
        # or the one picked by --resume/--continue); the TUI must open on
        # THAT thread. Passing it down to ChatPane as a required constructor
        # argument — rather than leaving the reactive at its None default and
        # hoping the user clicks a session — is what keeps every turn from
        # landing on a literal graph thread named "None".
        #
        # NB: *not* `self._thread_id` — Textual's MessagePump already owns
        # that attribute (it re-stamps it with `threading.get_ident()` when
        # the app starts running), so storing the graph thread there would be
        # silently overwritten with an OS thread id before compose() runs.
        self._start_thread_id = thread_id
        self.client = LunaClient(base_url, token)
        #: Last seen status of every background run (by session) and task (by handle).
        self._runs: dict[str, str] = {}
        self._tasks: dict[str, str] = {}
        self._task_labels: dict[str, str] = {}
        #: Task handles whose completion was already toasted.
        self._announced: set[str] = set()
        #: Poll on the next tick even if nothing looked live (a run was just started).
        self._poll_armed = True

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Make Luna's palette available as ``$bg``/``$peri``/etc. everywhere.

        Textual resolves these against *every* stylesheet the app loads —
        ``luna.tcss`` here, but also any future widget-bundled
        ``DEFAULT_CSS`` — without needing the declaration repeated in each
        one, unlike the old ``$var: value;``-string-concatenation approach.
        """
        return TUI_VARIABLES

    def compose(self) -> ComposeResult:
        """Build the layout: sessions sidebar, chat pane, footer."""
        # SessionsSidebar's __init__ doesn't forward an `id=` kwarg to
        # Widget.__init__, so the id is assigned on the instance instead —
        # DOMNode.id is settable once, before mount — to keep matching the
        # #sessions-sidebar CSS selector and the widget-lookup tests.
        sessions_sidebar = SessionsSidebar(
            workdir=self._workdir, current_thread_id=self._start_thread_id
        )
        sessions_sidebar.id = "sessions-sidebar"
        with Horizontal():
            yield sessions_sidebar
            # The status bar is one of ChatPane's own children now (right
            # under its input), not a separate full-width bar spanning
            # under both sidebars — the user explicitly wanted model/
            # provider/usage sitting directly under the text they type
            # into, the same place a terminal coding CLI usually shows it,
            # not a strip that happens to also run under the session list.
            yield ChatPane(
                workdir=self._workdir,
                thread_id=self._start_thread_id,
                provider=self._provider,
                model=self._model,
                pricing=self._pricing,
            )
        yield Footer()

    async def on_mount(self) -> None:
        """Populate the session list, then focus the chat input.

        Textual's own AUTO_FOCUS picks the first *focusable* widget in DOM
        order, which is the sessions ListView (it mounts before ChatPane's
        Input) — not the input a chat app should open ready-to-type in.
        Without this, every keystroke typed on launch lands in the session
        list instead of the input, silently doing nothing.
        """
        await self.query_one(SessionsSidebar).refresh_sessions()
        self.query_one("#chat-input").focus()
        self.set_interval(3.0, self._tick)

    async def _tick(self) -> None:
        """Poll ``/agents`` every 3 s while something runs (or was just started)."""
        live = any(s in self._LIVE for s in (*self._runs.values(), *self._tasks.values()))
        if live or self._poll_armed or self.query_one(SessionsSidebar).any_running:
            self._poll_armed = False
            await self.poll_agents()

    async def poll_agents(self) -> None:
        """Announce background runs and tasks that finished since the last poll."""
        try:
            agents = await self.client.agents()
        except ServerError:
            return
        changed = False
        for run in agents.get("runs", []):
            before = self._runs.get(run["session_id"])
            self._runs[run["session_id"]] = run["status"]
            changed = changed or before != run["status"]
            if before in self._LIVE and run["status"] in self._VERBS:
                self.notify(
                    f"Фоновая сессия «{run['label']}» {self._VERBS[run['status']]}",
                    severity="error" if run["status"] == "failed" else "information",
                    markup=False,
                )
        for task in agents.get("tasks", []):
            handle = task["handle"]
            before = self._tasks.get(handle)
            self._tasks[handle] = task["status"]
            self._task_labels[handle] = task["label"]
            if before in self._LIVE and task["status"] in self._VERBS:
                self._announce_task(handle, task["status"])
        if changed:
            await self.query_one(SessionsSidebar).refresh_sessions()
        chat = self.query_one(ChatPane)
        asks = any(
            r["session_id"] == chat.thread_id and r["status"] == "waiting"
            for r in agents.get("runs", [])
        ) or any(
            t["session_id"] == chat.thread_id and t["status"] in self._LIVE
            for t in agents.get("tasks", [])
        )
        if asks:
            chat.watch_out_of_turn()

    def _announce_task(self, handle: str, status: str) -> None:
        if handle in self._announced:
            return
        self._announced.add(handle)
        label = self._task_labels.get(handle, handle)
        self.notify(
            f"Фоновая задача «{label}» {self._VERBS.get(status, status)}",
            severity="error" if status == "failed" else "information",
            markup=False,
        )

    def announce_background_done(self, handle: str, ok: bool) -> None:
        """Toast a ``background_done`` event (once per task, polling may see it too)."""
        self._tasks[handle] = "done" if ok else "failed"
        self._announce_task(handle, "done" if ok else "failed")

    async def action_send_background(self) -> None:
        """Ctrl+G: run the typed prompt in the background in this session."""
        chat = self.query_one(ChatPane)
        chat_input = self.query_one("#chat-input")
        text = chat_input.value.strip()
        if not text:
            self.notify("Введите задачу, затем Ctrl+G.", severity="warning")
            return
        if chat.busy:
            self.notify("Дождитесь окончания ответа.", severity="warning")
            return
        chat_input.value = ""
        self._poll_armed = True
        await chat.send_background(text)

    async def on_sessions_sidebar_session_selected(
        self, event: SessionsSidebar.SessionSelected
    ) -> None:
        """Switch the chat pane to the picked session's thread and reload it."""
        await self._open_thread(event.thread_id)

    async def on_sessions_sidebar_new_session_requested(
        self, event: SessionsSidebar.NewSessionRequested
    ) -> None:
        """Handle the "+ новая сессия" row exactly like Ctrl+N."""
        await self.action_new_session()

    async def action_new_session(self) -> None:
        """Start a fresh thread in this project (Ctrl+N)."""
        if self._turn_running():
            return
        try:
            thread_id = await self.client.create_session(self._workdir)
        except ServerError as exc:
            self.notify(readable(exc), severity="error", markup=False)
            return
        await self._open_thread(thread_id)

    async def on_chat_pane_switch_session(self, event: ChatPane.SwitchSession) -> None:
        """Open the session a command (``/fork``) moved the conversation to."""
        await self._open_thread(event.session_id, check_busy=False)
        await self.query_one(SessionsSidebar).refresh_sessions()

    async def on_chat_pane_turn_finished(self, event: ChatPane.TurnFinished) -> None:
        """Re-fetch the list so a new session appears and the order updates."""
        await self.query_one(SessionsSidebar).refresh_sessions()

    @work
    async def _ask(self, screen):
        """Show a modal and wait for its answer (push_screen_wait needs a worker)."""
        return await self.push_screen_wait(screen)

    def _turn_running(self) -> bool:
        """Refuse (with a notice) to switch sessions while a turn is streaming."""
        if self.query_one(ChatPane).busy:
            self.notify("Дождитесь окончания ответа, затем переключайте сессию.")
            return True
        return False

    async def _open_thread(self, thread_id: str, *, check_busy: bool = True) -> None:
        """Point the chat at ``thread_id``, replay its history, sync the highlight.

        Without the reload, the transcript kept showing whatever the
        previously-open session had streamed into it.
        """
        if check_busy and self._turn_running():
            return
        chat = self.query_one(ChatPane)
        chat.stop_watching()
        chat.thread_id = thread_id
        await chat.load_history()
        await chat.refresh_state()
        self.query_one(SessionsSidebar).set_current(thread_id)
        self.query_one("#chat-input").focus()

    def action_toggle_panels(self) -> None:
        """Show/hide the sessions sidebar (Ctrl+B)."""
        sidebar = self.query_one("#sessions-sidebar")
        sidebar.display = not sidebar.display

    async def on_unmount(self) -> None:
        """Close the server client's HTTP connection when the app exits."""
        await self.client.aclose()
