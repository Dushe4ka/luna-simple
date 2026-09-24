"""The center chat pane: streamed Markdown transcript + slash-command input."""

from __future__ import annotations

from textual import work
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, ListItem, ListView, Markdown

from luna.tui.commands import filter_commands
from luna.tui.sidebar_activity import ActivitySidebar


class ChatPane(Widget):
    """Chat transcript + input row, streamed via the server's SSE endpoint."""

    thread_id: reactive[str | None] = reactive(None)

    def __init__(self, *, workdir: str, thread_id: str) -> None:
        super().__init__()
        self._workdir = workdir
        # Required, not defaulted: the pane must always open on a real
        # thread id resolved by the CLI. Leaving the reactive at its None
        # default made every fresh session address a graph thread literally
        # named "None" (and silently discarded --resume/--continue).
        self.thread_id = thread_id

    def compose(self):
        """Build the transcript, autocomplete dropdown, and chat input."""
        with Vertical():
            yield Markdown(id="transcript")
            yield ListView(id="autocomplete")
            yield Input(placeholder="> ", id="chat-input")

    def on_mount(self) -> None:
        """Hide the autocomplete dropdown until there's a slash prefix to match."""
        self.query_one("#autocomplete", ListView).display = False

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
        for name, text in matches:
            dropdown.append(ListItem(_CommandLabel(name, text)))
        dropdown.display = bool(matches)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Send the message and stream the response into the transcript."""
        if event.input.id != "chat-input":
            return
        content = event.value
        event.input.value = ""
        self.query_one("#autocomplete", ListView).display = False
        transcript = self.query_one("#transcript", Markdown)
        stream = Markdown.get_stream(transcript)
        app = self.app
        try:
            async for evt in app.client.send_message(self.thread_id, content, self._workdir):
                if evt["event"] == "text_delta":
                    await stream.write(evt["text"])
                elif evt["event"] == "tool_started":
                    activity = app.query_one(ActivitySidebar)
                    activity.tool_started(evt["call_id"], evt["name"], evt["args"])
                elif evt["event"] == "tool_finished":
                    activity = app.query_one(ActivitySidebar)
                    activity.tool_finished(evt["call_id"], evt["name"], evt["ok"], evt["detail"])
                elif evt["event"] == "approval_needed":
                    requests = evt["value"].get("action_requests") or [
                        evt["value"].get("action_request")
                    ]
                    # ModalScreen.push_screen_wait requires worker context
                    # (Textual raises NoActiveWorker otherwise), so the
                    # actual push+wait is delegated to the @work-decorated
                    # _await_approval_decision helper below. on_input_submitted
                    # itself stays a plain coroutine so it's still directly
                    # awaitable (Task 8's regression test calls it that way)
                    # and its try/finally around stream.stop() is unaffected.
                    decision = await self._await_approval_decision(requests[0]).wait()
                    async for resume_evt in app.client.approve(
                        self.thread_id, decision, self._workdir
                    ):
                        if resume_evt["event"] == "text_delta":
                            await stream.write(resume_evt["text"])
                        elif resume_evt["event"] == "tool_started":
                            activity = app.query_one(ActivitySidebar)
                            activity.tool_started(
                                resume_evt["call_id"], resume_evt["name"], resume_evt["args"]
                            )
                        elif resume_evt["event"] == "tool_finished":
                            activity = app.query_one(ActivitySidebar)
                            activity.tool_finished(
                                resume_evt["call_id"],
                                resume_evt["name"],
                                resume_evt["ok"],
                                resume_evt["detail"],
                            )
        finally:
            await stream.stop()

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
