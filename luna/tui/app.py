"""Luna's full-screen Textual TUI application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer

from luna.server.client import ServerClient
from luna.tui.chat import ChatPane
from luna.tui.sidebar_activity import ActivitySidebar
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

    BINDINGS = [("ctrl+b", "toggle_panels", "Toggle panels")]

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
        self.client = ServerClient(base_url=base_url, token=token)

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Make Luna's palette available as ``$bg``/``$peri``/etc. everywhere.

        Textual resolves these against *every* stylesheet the app loads —
        ``luna.tcss`` here, but also any future widget-bundled
        ``DEFAULT_CSS`` — without needing the declaration repeated in each
        one, unlike the old ``$var: value;``-string-concatenation approach.
        """
        return TUI_VARIABLES

    def compose(self) -> ComposeResult:
        """Build the layout: two sidebars, the chat pane, and the footer."""
        # SessionsSidebar/ActivitySidebar's __init__ signatures (verbatim from
        # the task brief) don't forward an `id=` kwarg to Widget.__init__, so
        # the id is assigned on the instance instead — DOMNode.id is settable
        # once, before mount — to keep matching the existing
        # #sessions-sidebar/#activity-sidebar CSS selectors and the Task 7
        # widget-lookup test.
        sessions_sidebar = SessionsSidebar(workdir=self._workdir)
        sessions_sidebar.id = "sessions-sidebar"
        activity_sidebar = ActivitySidebar()
        activity_sidebar.id = "activity-sidebar"
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
            yield activity_sidebar
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

    async def on_sessions_sidebar_session_selected(
        self, event: SessionsSidebar.SessionSelected
    ) -> None:
        """Switch the chat pane to the clicked session's thread and reload it.

        Without the reload, the transcript kept showing whatever the
        previously-open session had streamed into it.
        """
        chat = self.query_one(ChatPane)
        chat.thread_id = event.thread_id
        await chat.load_history()
        self.query_one("#chat-input").focus()

    def action_toggle_panels(self) -> None:
        """Show/hide the two sidebars (Ctrl+B)."""
        for widget_id in ("#sessions-sidebar", "#activity-sidebar"):
            widget = self.query_one(widget_id)
            widget.display = not widget.display

    async def on_unmount(self) -> None:
        """Close the server client's HTTP connection when the app exits."""
        await self.client.aclose()
