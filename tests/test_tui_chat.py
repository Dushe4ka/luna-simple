import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input
from textual.widgets.markdown import MarkdownStream

from luna.tui.chat import ChatPane
from luna.tui.commands import filter_commands


def test_filter_commands_matches_prefix():
    results = filter_commands("/cle")
    names = [r[0] for r in results]
    assert all(n.startswith("/cle") for n in names)
    assert len(results) >= 1


def test_filter_commands_empty_slash_returns_all():
    results = filter_commands("/")
    from luna.repl.commands import HELP

    assert len(results) == len(HELP)


def test_filter_commands_no_match_returns_empty():
    assert filter_commands("/zzz-nope") == []


class _FakeClient:
    """Sends one text_delta then raises, mimicking a dropped SSE connection."""

    async def send_message(self, thread_id, content, workdir):
        yield {"event": "text_delta", "text": "partial "}
        raise RuntimeError("connection dropped mid-stream")


class _HarnessApp(App):
    """Minimal app hosting a ChatPane, with a fake server client attached."""

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client

    def compose(self) -> ComposeResult:
        yield ChatPane(workdir=".")


async def test_markdown_stream_stopped_even_when_sse_stream_raises():
    """A dropped/erroring SSE stream must not leak MarkdownStream's
    background task — stream.stop() must still run via `finally`.
    """
    app = _HarnessApp(_FakeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        chat.thread_id = "t1"

        stop_calls: list[MarkdownStream] = []
        original_stop = MarkdownStream.stop

        async def spy_stop(self):
            stop_calls.append(self)
            await original_stop(self)

        MarkdownStream.stop = spy_stop
        try:
            inp = chat.query_one("#chat-input", Input)
            inp.value = "hello"
            with pytest.raises(RuntimeError, match="connection dropped mid-stream"):
                await chat.on_input_submitted(Input.Submitted(inp, "hello"))
        finally:
            MarkdownStream.stop = original_stop

        assert len(stop_calls) == 1
