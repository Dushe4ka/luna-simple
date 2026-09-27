"""The center chat pane: streamed Markdown transcript + slash-command input."""

from __future__ import annotations

from textual import events, work
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, ListItem, ListView, Markdown
from textual.widgets.markdown import MarkdownStream

from luna.config.usage import SessionUsage, TurnUsage, indicator_line
from luna.tui.banner import LunaBanner
from luna.tui.commands import HELP, filter_commands
from luna.tui.sidebar_activity import ActivitySidebar
from luna.tui.status_bar import StatusBar
from luna.tui.widgets import PulseGlyph

#: Commands handled locally, without ever reaching the agent — each needs
#: no server/agent state, only the TUI's own widgets. Everything else in
#: HELP gets an honest "not yet in the TUI" message on submit instead.
_LOCALLY_SUPPORTED = ("/exit", "/quit", "/clear", "/help", "/commands")


class UserMessage(Markdown):
    """One "You" turn — its own colored panel, distinct from Luna's replies."""


class LunaMessage(Markdown):
    """One "Luna" turn — its own colored panel, distinct from the user's."""


class SystemMessage(Markdown):
    """Local-command output (help text, "not yet in the TUI" notices) — neutral, unlabeled."""


def _help_markdown() -> str:
    """Render the full command list as markdown, ready to show as a SystemMessage."""
    lines = [f"- `{name}` — {text}" for name, text in HELP.items()]
    return "**Команды**\n\n" + "\n".join(lines)


def render_history(messages: list[dict]) -> list[Markdown]:
    """Build one styled message widget per turn, ready to mount into the transcript.

    Mirrors the live turn's own UserMessage/LunaMessage split (see
    ``ChatPane.on_input_submitted``) so replaying history looks identical
    to having watched it happen live.
    """
    widgets: list[Markdown] = []
    for m in messages:
        if m["role"] == "human":
            widgets.append(UserMessage(f"**You**\n\n{m['content']}"))
        else:
            widgets.append(LunaMessage(f"**Luna**\n\n{m['content']}"))
    return widgets


class _LiveReply:
    """Lazily creates Luna's reply widget on the turn's first bit of content.

    Keeps the "thinking" placeholder visible until there is actually
    something to show — a turn that only calls tools and never speaks
    would otherwise flash an empty "**Luna**" panel for no reason. The
    underlying ``MarkdownStream`` is created once and reused across an
    entire turn, including any approval round-trips, then stopped exactly
    once in ``stop()``.
    """

    def __init__(self, transcript: VerticalScroll, thinking: PulseGlyph) -> None:
        self._transcript = transcript
        self._thinking = thinking
        self._response: LunaMessage | None = None
        self._stream: MarkdownStream | None = None

    async def write(self, text: str) -> None:
        if self._stream is None:
            if self._thinking.is_mounted:
                await self._thinking.remove()
            self._response = LunaMessage("**Luna**\n\n")
            await self._transcript.mount(self._response)
            self._transcript.anchor()
            self._stream = Markdown.get_stream(self._response)
        await self._stream.write(text)

    async def stop(self) -> None:
        if self._stream is not None:
            await self._stream.stop()
        elif self._thinking.is_mounted:
            await self._thinking.remove()


class ChatPane(Widget):
    """Chat transcript + input row, streamed via the server's SSE endpoint.

    Styling for this widget lives in ``luna/tui/luna.tcss`` (external
    stylesheet, loaded by ``LunaApp.CSS_PATH``), not a ``DEFAULT_CSS``
    string here — see that file's header comment for why.
    """

    thread_id: reactive[str | None] = reactive(None)

    def __init__(
        self,
        *,
        workdir: str,
        thread_id: str,
        provider: str = "",
        model: str | None = None,
        pricing: dict | None = None,
    ) -> None:
        super().__init__()
        self._workdir = workdir
        # Required, not defaulted: the pane must always open on a real
        # thread id resolved by the CLI. Leaving the reactive at its None
        # default made every fresh session address a graph thread literally
        # named "None" (and silently discarded --resume/--continue).
        self.thread_id = thread_id
        self._provider = provider
        self._model = model or ""
        self._pricing = pricing or {}
        # Accumulates every completed turn's token counts (see
        # luna.config.usage — the same accounting the old line REPL showed
        # via its post-turn "ctx ~X/Y · ..." indicator, now surfaced in the
        # status bar instead of being silently dropped by the TUI).
        self._session_usage = SessionUsage()

    def compose(self):
        """Build the transcript, autocomplete dropdown, input, and status bar.

        The status bar lives here — right under the input, in ChatPane's
        own column — rather than as a separate strip spanning under both
        sidebars: model/provider/usage belongs directly under the text
        someone is about to send, the way a terminal coding CLI usually
        shows it.
        """
        with Vertical():
            yield VerticalScroll(id="transcript")
            yield ListView(id="autocomplete")
            yield Input(placeholder="> ", id="chat-input")
            yield StatusBar(id="status-bar")

    async def on_mount(self) -> None:
        """Hide the dropdown, load history, show the banner, prime the status bar.

        Without the history load, opening (or switching to) a session
        showed a blank transcript until the next new message — the prior
        conversation never appeared at all. The banner is mounted *after*
        history (then moved to index 0), not before — ``load_history``
        unconditionally clears the transcript first (also true on a later
        session switch), which would otherwise delete it again straight
        away. It is shown exactly once, for the thread this TUI process
        actually opened on — switching sessions via the sidebar calls
        ``load_history`` again on its own and does not re-add it, the same
        way Claude Code's own startup banner doesn't reprint per message.
        """
        self.query_one("#autocomplete", ListView).display = False
        await self.load_history()
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(LunaBanner(), before=0)
        self._refresh_status_bar()

    def _refresh_status_bar(self) -> None:
        """Push the current model/provider/usage figures into the status bar.

        Called on mount (so model/provider are visible before the first
        message is ever sent) and again once a turn finishes accumulating
        usage — never mid-stream, matching the old REPL's own cadence
        (`session.py` only ever printed its usage indicator once a turn
        had fully completed, not per streamed token).
        """
        status_bar = self.query_one(StatusBar)
        status_bar.model = self._model
        status_bar.provider = self._provider
        if self._session_usage.turns:
            status_bar.usage_summary = indicator_line(
                self._session_usage, self._provider, self._model, self._pricing
            )

    async def load_history(self) -> None:
        """Fetch this thread's prior turns and replace the transcript with them.

        Called on mount and whenever the sidebar switches ``thread_id`` to a
        different session — both cases must show that thread's own history,
        not whatever the previously-open session had streamed into the
        widget.
        """
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        messages = await self.app.client.get_history(self.thread_id, self._workdir)
        if messages:
            await transcript.mount_all(render_history(messages))
            transcript.anchor()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter and show/hide the slash-command dropdown as the input changes."""
        if event.input.id != "chat-input":
            return
        value = event.value
        dropdown = self.query_one("#autocomplete", ListView)
        if not value.startswith("/"):
            dropdown.display = False
            return
        matches = filter_commands(value)
        dropdown.clear()
        # display/height are set BEFORE the items are appended, not after:
        # a `display: none` widget skips layout entirely, so a ListItem
        # mounted while the dropdown is still hidden gets stuck at its
        # measured `height: auto` of 0 forever — flipping `display` back on
        # afterwards does not retroactively re-measure already-mounted
        # children. Confirmed empirically: with the old append-then-show
        # order, `dropdown.region.height` (and the container's `styles.
        # height`) were correct, but every individual ListItem's own
        # `.region.height` stayed 0 — a fully populated, correctly-sized,
        # completely invisible dropdown, which unit tests asserting only on
        # the container's region never caught.
        dropdown.display = bool(matches)
        # ListView's own auto-height doesn't reserve real space for its
        # content (see the DEFAULT_CSS comment on #autocomplete), so the
        # visible height is set here instead — one row per match, capped at
        # 10 so a broad prefix like "/" doesn't take over the screen.
        dropdown.styles.height = min(len(matches), 10)
        for name, text in matches:
            # _CommandLabel IS a ListItem (see its definition below) — it
            # must be appended directly, NOT wrapped in another `ListItem(...)`.
            # That double nesting (confirmed empirically: a `ListItem`
            # containing a `ListItem` containing the real `Label`) is what
            # made the whole dropdown invisible: every row's own `height:
            # auto` resolved to 0 regardless of its Label's real content
            # height, while the *container*'s explicit `styles.height` above
            # stayed correct — a fully populated, correctly-sized ListView
            # with 24 zero-height rows in it, empty to the eye. The
            # container-only assertions in the existing tests never caught
            # this; only checking each item's own `.region` did.
            item = _CommandLabel(name, text)
            item.data_command_name = name  # plain attribute, no reactive needed here
            dropdown.append(item)
        if matches:
            # Pre-select the first suggestion so Up/Down/Enter work
            # immediately, the way a terminal autocomplete is expected to.
            dropdown.index = 0

    def on_key(self, event: events.Key) -> None:
        """Let Up/Down/Escape drive the slash-command dropdown while it's open.

        `Input` itself binds neither `up`/`down`/`escape` (verified against
        its real BINDINGS), so these bubble here unhandled — `enter` is the
        one exception, handled separately in `on_input_submitted` since
        `Input` already binds it to submit.
        """
        dropdown = self.query_one("#autocomplete", ListView)
        if not dropdown.display:
            return
        if event.key == "down":
            dropdown.action_cursor_down()
            event.stop()
        elif event.key == "up":
            dropdown.action_cursor_up()
            event.stop()
        elif event.key == "escape":
            dropdown.display = False
            event.stop()

    def _accept_highlighted_command(self) -> bool:
        """Fill the input with the dropdown's highlighted command, if open.

        Returns True if it did — the caller (`on_input_submitted`) treats
        that as "don't send this turn, the user was picking a command."
        """
        dropdown = self.query_one("#autocomplete", ListView)
        if not dropdown.display:
            return False
        highlighted = dropdown.highlighted_child
        name = getattr(highlighted, "data_command_name", None)
        if name is None:
            return False
        chat_input = self.query_one("#chat-input", Input)
        chat_input.value = f"{name} "
        chat_input.cursor_position = len(chat_input.value)
        dropdown.display = False
        return True

    async def _run_local_command(self, content: str, transcript: VerticalScroll) -> None:
        """Handle a "/"-prefixed line locally — it must never reach the agent.

        Mirrors ``luna.repl.commands.dispatch``'s own rule: any line
        starting with "/" is *always* a command attempt, recognized or
        not, and is never sent to the model as a chat message. Before
        this, the TUI sent every "/" line straight through — a real
        incident: submitting ``/quit`` got the model role-playing a reply
        ("Пока! Обращайся, когда понадобится.") as if it were small talk,
        instead of actually exiting. Only the handful of commands that
        need no agent/server state are implemented here; everything else
        in ``HELP`` gets an honest "not yet in the TUI" message.
        """
        name, _, _ = content.partition(" ")
        if name in ("/exit", "/quit"):
            self.app.exit()
        elif name == "/clear":
            await transcript.remove_children()
        elif name in ("/help", "/commands"):
            await transcript.mount(SystemMessage(_help_markdown()))
            transcript.anchor()
        else:
            supported = ", ".join(f"`{n}`" for n in _LOCALLY_SUPPORTED)
            await transcript.mount(
                SystemMessage(
                    f"*`{name}` пока не работает в TUI — сейчас доступны "
                    f"{supported}. Остальные команды есть в построчном REPL "
                    f"(`luna` без полноэкранного режима).*"
                )
            )
            transcript.anchor()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Send the message and stream the response into the transcript."""
        if event.input.id != "chat-input":
            return
        if self._accept_highlighted_command():
            return
        content = event.value
        event.input.value = ""
        self.query_one("#autocomplete", ListView).display = False
        transcript = self.query_one("#transcript", VerticalScroll)
        if content.startswith("/"):
            await self._run_local_command(content, transcript)
            return
        # Echo the user's own message into the transcript immediately —
        # before it, only the assistant's streamed text ever appeared, so
        # submitting a message that got a slow (or empty) reply looked
        # exactly like nothing had happened at all. A blinking placeholder
        # takes the assistant's spot right away too, so waiting for the
        # first token (or a tool call) never looks like the TUI has frozen.
        await transcript.mount(UserMessage(f"**You**\n\n{content}"))
        thinking = PulseGlyph("Luna думает")
        await transcript.mount(thinking)
        transcript.anchor()
        reply = _LiveReply(transcript, thinking)
        # One accumulator for the whole turn (including any approval
        # round-trips) — merged from each `usage_delta` event, then folded
        # into the session total exactly once, on `turn_done`.
        turn_usage = TurnUsage()
        app = self.app
        try:
            # Resolved inside the try (not before it) so that even a failed
            # lookup still runs `finally: reply.stop()` — Task 8's guarantee.
            activity = app.query_one(ActivitySidebar)
            # A turn can pause for approval more than once: the server's SSE
            # stream always ends after an Interrupted, so a resumed stream
            # that hits a *second* interrupt ends on another approval_needed.
            # Loop until a stream finishes without one — mirroring
            # luna.core.session._stream_turn's own `while True`. Handling only
            # one round (the old nested `async for`) left the graph paused on
            # the second interrupt with nothing in the UI to resume it.
            event_stream = app.client.send_message(self.thread_id, content, self._workdir)
            while True:
                approval_value = None
                async for evt in event_stream:
                    pending = await self._apply_event(evt, reply, activity, turn_usage)
                    if pending is not None:
                        approval_value = pending
                if approval_value is None:
                    break
                requests = approval_value.get("action_requests") or [
                    approval_value.get("action_request")
                ]
                # ModalScreen.push_screen_wait requires worker context
                # (Textual raises NoActiveWorker otherwise), so the actual
                # push+wait is delegated to the @work-decorated
                # _await_approval_decision helper below. on_input_submitted
                # itself stays a plain coroutine so it's still directly
                # awaitable (Task 8's regression test calls it that way) and
                # its try/finally around reply.stop() is unaffected.
                decision = await self._await_approval_decision(requests[0]).wait()
                event_stream = app.client.approve(self.thread_id, decision, self._workdir)
        finally:
            await reply.stop()

    async def _apply_event(
        self, evt: dict, reply: _LiveReply, activity: ActivitySidebar, turn_usage: TurnUsage
    ) -> dict | None:
        """Apply one SSE event to the transcript/activity sidebar/status bar.

        Returns the interrupt's action-request payload if ``evt`` is an
        ``approval_needed`` event, else ``None``.
        """
        if evt["event"] == "text_delta":
            await reply.write(evt["text"])
        elif evt["event"] == "usage_delta":
            turn_usage.merge(evt["usage_metadata"])
        elif evt["event"] == "turn_done":
            # Folded into the session total (and the status bar refreshed)
            # exactly once per completed turn — never mid-stream, matching
            # the cadence `session.py`'s own post-turn indicator line used.
            self._session_usage.add_turn(turn_usage)
            self._refresh_status_bar()
        elif evt["event"] == "tool_started":
            activity.tool_started(evt["call_id"], evt["name"], evt["args"])
        elif evt["event"] == "tool_finished":
            activity.tool_finished(evt["call_id"], evt["name"], evt["ok"], evt["detail"])
        elif evt["event"] == "approval_needed":
            return evt["value"]
        elif evt["event"] == "error":
            # Without this, a turn that fails server-side (a provider call
            # erroring, an unhandled exception) left the transcript exactly
            # as it was before the message was sent — no different from
            # the turn still being in progress, forever.
            await reply.write(f"\n\n**⚠ Error:** {evt['message']}\n\n")
        return None

    @work
    async def _await_approval_decision(self, action_request: dict) -> dict:
        """Push the approval modal and block until the user dismisses it.

        Runs as a Textual worker solely because ``push_screen_wait``
        requires worker context to await a screen's dismissal without
        stalling the app; the caller awaits this method's returned
        ``Worker`` explicitly (``.wait()``) instead of ``await``-ing this
        method directly.
        """
        from luna.tui.approval_modal import ApprovalModal

        return await self.app.push_screen_wait(ApprovalModal(action_request))


class _CommandLabel(ListItem):
    def __init__(self, name: str, text: str) -> None:
        super().__init__()
        self._name = name
        self._text = text

    def compose(self):
        from textual.widgets import Label

        yield Label(f"{self._name}  {self._text}")
