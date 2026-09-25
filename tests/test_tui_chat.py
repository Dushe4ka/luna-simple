import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input
from textual.widgets.markdown import MarkdownStream

from luna.tui.chat import ChatPane
from luna.tui.commands import filter_commands
from luna.tui.sidebar_activity import ActivitySidebar


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
        yield ChatPane(workdir=".", thread_id="t1")
        # on_input_submitted resolves the activity sidebar once per turn
        # (instead of re-querying it per event), so the harness must host one.
        yield ActivitySidebar()


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


class _ApprovalFakeClient:
    """Emits an approval_needed event, then (once resumed) a text_delta."""

    def __init__(self) -> None:
        self.approve_calls: list[tuple[str, dict, str]] = []

    async def send_message(self, thread_id, content, workdir):
        yield {"event": "text_delta", "text": "before "}
        yield {
            "event": "approval_needed",
            "value": {
                "action_requests": [
                    {"action": "write_file", "args": {"file_path": "/a.py", "content": "x"}}
                ]
            },
        }

    async def approve(self, thread_id, decision, workdir):
        self.approve_calls.append((thread_id, decision, workdir))
        yield {"event": "text_delta", "text": "after "}


async def test_approval_needed_pushes_modal_and_resumes_with_decision():
    """The approval_needed branch pushes ApprovalModal, then feeds the
    resume stream's events into the same transcript — all still inside
    on_input_submitted's try/finally (stream.stop() still runs once).
    """
    client = _ApprovalFakeClient()
    app = _HarnessApp(client)
    async with app.run_test() as pilot:
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
            task = asyncio.create_task(chat.on_input_submitted(Input.Submitted(inp, "hello")))
            await pilot.pause()
            await pilot.click("#approve-button")
            await asyncio.wait_for(task, timeout=5)
        finally:
            MarkdownStream.stop = original_stop

        assert len(stop_calls) == 1
        assert client.approve_calls == [("t1", {"type": "approve"}, ".")]


class _TwoApprovalFakeClient:
    """A turn that pauses for approval TWICE before finishing.

    The server's SSE stream always ends after an ``Interrupted``, so a
    resumed stream that hits a second interrupt ends on another
    ``approval_needed`` — exactly what this fake reproduces.
    """

    def __init__(self) -> None:
        self.approve_calls: list[tuple[str, dict, str]] = []

    async def send_message(self, thread_id, content, workdir):
        yield {"event": "text_delta", "text": "first "}
        yield {
            "event": "approval_needed",
            "value": {
                "action_requests": [
                    {"action": "write_file", "args": {"file_path": "/one.py", "content": "1"}}
                ]
            },
        }

    async def approve(self, thread_id, decision, workdir):
        self.approve_calls.append((thread_id, decision, workdir))
        if len(self.approve_calls) == 1:
            yield {"event": "text_delta", "text": "second "}
            yield {
                "event": "approval_needed",
                "value": {
                    "action_requests": [
                        {"action": "write_file", "args": {"file_path": "/two.py", "content": "2"}}
                    ]
                },
            }
        else:
            yield {"event": "text_delta", "text": "done"}


async def test_two_sequential_approvals_in_one_turn_both_get_resumed():
    """Regression (C2): a turn needing two approvals used to be stranded.

    The old nested `async for` handled exactly one round: when the resumed
    stream itself ended on a second ``approval_needed``, that event was
    dropped on the floor, the already-exhausted outer loop ended, and the
    graph stayed paused forever with nothing in the UI to resume it.
    """
    client = _TwoApprovalFakeClient()
    app = _HarnessApp(client)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)

        stop_calls: list[MarkdownStream] = []
        written: list[str] = []
        original_stop = MarkdownStream.stop
        original_write = MarkdownStream.write

        async def spy_stop(self):
            stop_calls.append(self)
            await original_stop(self)

        async def spy_write(self, text):
            written.append(text)
            await original_write(self, text)

        MarkdownStream.stop = spy_stop
        MarkdownStream.write = spy_write
        try:
            inp = chat.query_one("#chat-input", Input)
            inp.value = "hello"
            task = asyncio.create_task(chat.on_input_submitted(Input.Submitted(inp, "hello")))
            await pilot.pause()
            await pilot.click("#approve-button")  # first approval
            await pilot.pause()
            await pilot.click("#approve-button")  # second approval
            await asyncio.wait_for(task, timeout=5)
        finally:
            MarkdownStream.stop = original_stop
            MarkdownStream.write = original_write

    assert client.approve_calls == [
        ("t1", {"type": "approve"}, "."),
        ("t1", {"type": "approve"}, "."),
    ]
    # the whole turn, including everything after the SECOND approval
    assert "".join(written) == "first second done"
    assert len(stop_calls) == 1
