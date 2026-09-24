"""The center chat pane: streamed Markdown transcript + slash-command input."""

from __future__ import annotations

from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, ListItem, ListView, Markdown

from luna.tui.commands import filter_commands


class ChatPane(Widget):
    """Chat transcript + input row, streamed via the server's SSE endpoint."""

    thread_id: reactive[str | None] = reactive(None)

    def __init__(self, *, workdir: str) -> None:
        super().__init__()
        self._workdir = workdir

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
        async for evt in app.client.send_message(self.thread_id, content, self._workdir):
            if evt["event"] == "text_delta":
                await stream.write(evt["text"])
        await stream.stop()


class _CommandLabel(ListItem):
    def __init__(self, name: str, text: str) -> None:
        super().__init__()
        self._name = name
        self._text = text

    def compose(self):
        from textual.widgets import Label

        yield Label(f"{self._name}  {self._text}")
