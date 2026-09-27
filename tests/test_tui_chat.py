import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Input, Markdown
from textual.widgets.markdown import MarkdownStream

from luna.tui.chat import ChatPane, LunaMessage, UserMessage, render_history
from luna.tui.commands import filter_commands
from luna.tui.status_bar import StatusBar


def transcript_text(transcript: VerticalScroll) -> str:
    """Join every mounted message widget's ``.source`` in DOM (chronological) order.

    ``#transcript`` holds one Markdown subclass per turn (UserMessage /
    LunaMessage / SystemMessage) instead of one shared Markdown widget, so
    a plain ``.source`` lookup no longer exists on the container itself.
    """
    return "\n".join(child.source for child in transcript.query(Markdown))


async def test_render_history_labels_human_and_ai_turns_you_then_luna():
    """render_history builds one widget per turn — Markdown only parses its
    ``.source`` once mounted, so this must check the widgets live, inside a
    running app, rather than the pre-refactor plain-string return value.
    """
    widgets = render_history(
        [
            {"role": "human", "content": "fix the bug"},
            {"role": "ai", "content": "done"},
        ]
    )
    assert isinstance(widgets[0], UserMessage)
    assert isinstance(widgets[1], LunaMessage)

    app = _HarnessApp(_NeverCallMeClient())
    async with app.run_test():
        transcript = app.query_one(ChatPane).query_one("#transcript", VerticalScroll)
        await transcript.mount_all(widgets)

        assert "fix the bug" in widgets[0].source
        assert "done" in widgets[1].source
        messages = [w for w in transcript.children if w in widgets]
        assert messages == widgets  # human turn first, then Luna's reply
        assert widgets[0].source.startswith("› ")
        assert "**Luna**" not in widgets[1].source


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

    async def get_history(self, thread_id, workdir):
        return []

    async def send_message(self, thread_id, content, workdir):
        yield {"event": "text_delta", "text": "partial "}
        raise RuntimeError("connection dropped mid-stream")


class _HarnessApp(App):
    """Minimal app hosting a ChatPane, with a fake server client attached."""

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client

    def compose(self) -> ComposeResult:
        yield ChatPane(workdir=".", thread_id="t1", provider="anthropic", model="claude-sonnet-5")


class _NeverCallMeClient:
    """Fails the test the instant `send_message` is invoked."""

    async def get_history(self, thread_id, workdir):
        return []

    async def send_message(self, thread_id, content, workdir):
        raise AssertionError(f"a slash command must never be sent to the agent: {content!r}")
        yield  # pragma: no cover - unreachable, keeps this an async generator


async def test_quit_exits_the_app_without_ever_asking_the_agent():
    """Regression: reproduces a real incident where submitting `/quit`
    made the model role-play a reply ("Пока! Обращайся, когда
    понадобится.") instead of actually exiting — every "/" line was sent
    to the agent like any other message. `/quit` (and `/exit`) must close
    the app and must never reach `send_message`.
    """
    app = _HarnessApp(_NeverCallMeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        inp.value = "/quit"
        await chat.on_input_submitted(Input.Submitted(inp, "/quit"))
        assert app._exit is True


async def test_clear_command_empties_the_transcript_locally():
    app = _HarnessApp(_NeverCallMeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        transcript = chat.query_one("#transcript", VerticalScroll)
        await transcript.mount(UserMessage("› hello"))
        assert "hello" in transcript_text(transcript)

        inp = chat.query_one("#chat-input", Input)
        inp.value = "/clear"
        await chat.on_input_submitted(Input.Submitted(inp, "/clear"))

        assert list(transcript.children) == []


async def test_help_command_lists_commands_without_asking_the_agent():
    app = _HarnessApp(_NeverCallMeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        inp.value = "/help"
        await chat.on_input_submitted(Input.Submitted(inp, "/help"))

        transcript = chat.query_one("#transcript", VerticalScroll)
        text = transcript_text(transcript)
        assert "/clear" in text
        assert "/undo" in text


async def test_unsupported_command_gets_an_honest_message_not_the_model():
    """A real, known REPL command the TUI doesn't implement yet (e.g.
    `/undo`) must say so plainly — not silently masquerade as a chat
    message the model then answers as if it understood "/undo" as English.
    """
    app = _HarnessApp(_NeverCallMeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        inp.value = "/undo"
        await chat.on_input_submitted(Input.Submitted(inp, "/undo"))

        transcript = chat.query_one("#transcript", VerticalScroll)
        text = transcript_text(transcript)
        assert "/undo" in text
        assert "пока не работает" in text


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


async def test_users_own_message_is_echoed_into_the_transcript_immediately():
    """Regression: before this, only the assistant's streamed text ever
    appeared in the transcript — submitting a message that got a slow (or
    dropped, as here) reply looked exactly like nothing had happened.
    """
    app = _HarnessApp(_FakeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        chat.thread_id = "t1"
        inp = chat.query_one("#chat-input", Input)
        inp.value = "hello there"
        with pytest.raises(RuntimeError, match="connection dropped mid-stream"):
            await chat.on_input_submitted(Input.Submitted(inp, "hello there"))

        transcript = chat.query_one("#transcript", VerticalScroll)
        text = transcript_text(transcript)
        assert "hello there" in text
        assert text.index("hello there") < text.index("partial")


async def test_slash_dropdown_reserves_real_visible_height_for_every_match():
    """Regression: ListView's own `height: auto` does not reserve space for
    its actual content — confirmed by measuring the dropdown's rendered
    region with the real command list (23+ entries) populated: it came out
    to 2 rows regardless of a `max-height: 10` cap, an unreadable sliver
    indistinguishable from "nothing opened". The height must be set
    explicitly, in step with the real match count.
    """
    from textual.widgets import ListView

    app = _HarnessApp(_FakeClient())
    async with app.run_test(size=(120, 40)) as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        inp.focus()
        await pilot.press("/")

        dropdown = chat.query_one("#autocomplete", ListView)
        from luna.repl.commands import HELP

        expected_rows = min(len(HELP), 10)
        assert dropdown.region.height == expected_rows


async def test_slash_dropdown_rows_are_individually_visible_not_zero_height():
    """Regression: the *container*'s region being sized correctly (the
    test above) does not mean each row inside it is. A real bug slipped
    past every existing test here: `on_input_changed` appended
    `ListItem(_CommandLabel(name, text))` — but `_CommandLabel` already
    *is* a `ListItem`, so this wrapped one `ListItem` inside another. That
    double nesting made every row's own `height: auto` resolve to 0 no
    matter how many matches there were: a fully populated, correctly
    *container*-sized ListView with 24 zero-height rows in it — invisible
    to the user, and to every test that only asserted on the container's
    own `.region`.
    """
    from textual.widgets import ListView

    app = _HarnessApp(_FakeClient())
    async with app.run_test(size=(120, 40)) as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        inp.focus()
        await pilot.press("/")

        dropdown = chat.query_one("#autocomplete", ListView)
        rows = list(dropdown.children)
        assert rows, "the dropdown must actually contain rows to check"
        for row in rows[:5]:
            assert row.region.height > 0, f"{row!r} has zero height — invisible"


async def test_slash_dropdown_opens_prehighlighted_and_navigates_with_arrows():
    """Typing '/' must open the dropdown with the first match already
    highlighted, and Down/Up must move that highlight — Input itself binds
    neither key, so without ChatPane.on_key forwarding them to the
    ListView, arrow keys did nothing while the dropdown was open.
    """
    from textual.widgets import ListView

    app = _HarnessApp(_FakeClient())
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        inp.focus()
        await pilot.press("/")
        dropdown = chat.query_one("#autocomplete", ListView)
        assert dropdown.display is True
        assert dropdown.index == 0
        first_highlighted = dropdown.highlighted_child

        await pilot.press("down")
        assert dropdown.index == 1
        assert dropdown.highlighted_child is not first_highlighted

        await pilot.press("escape")
        assert dropdown.display is False


async def test_enter_on_open_dropdown_fills_input_instead_of_sending_a_turn():
    """Enter while the dropdown is open must accept the highlighted command
    into the input, not submit it as a chat turn — mirrors how a terminal
    command palette behaves (accept, then a second Enter actually runs it).
    """
    from textual.widgets import ListView

    app = _HarnessApp(_FakeClient())
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        inp.focus()
        for ch in "/cle":
            await pilot.press(ch)
        dropdown = chat.query_one("#autocomplete", ListView)
        assert dropdown.display is True

        sent = await chat.on_input_submitted(Input.Submitted(inp, inp.value))
        assert sent is None
        assert inp.value == "/clear "
        assert dropdown.display is False


class _ErrorFakeClient:
    """A turn that fails server-side: one text_delta, then an error event."""

    async def get_history(self, thread_id, workdir):
        return []

    async def send_message(self, thread_id, content, workdir):
        yield {"event": "text_delta", "text": "thinking... "}
        yield {"event": "error", "message": "RuntimeError: insufficient balance"}


async def test_error_event_is_rendered_visibly_not_silently_dropped():
    """Regression: before the server surfaced failures as an `error` event,
    a failed turn just left the transcript unchanged — indistinguishable
    from the turn still being in progress. This must not raise and must
    leave the error text in the transcript.
    """
    app = _HarnessApp(_ErrorFakeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        chat.thread_id = "t1"
        inp = chat.query_one("#chat-input", Input)
        inp.value = "hello"

        await chat.on_input_submitted(Input.Submitted(inp, "hello"))

        transcript = chat.query_one("#transcript", VerticalScroll)
        assert "insufficient balance" in transcript_text(transcript)


class _ApprovalFakeClient:
    """Emits an approval_needed event, then (once resumed) a text_delta."""

    def __init__(self) -> None:
        self.approve_calls: list[tuple[str, dict, str]] = []

    async def get_history(self, thread_id, workdir):
        return []

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

    async def get_history(self, thread_id, workdir):
        return []

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


async def test_status_bar_shows_model_and_provider_as_soon_as_the_chat_pane_mounts():
    """Regression: StatusBar was mounted but nothing ever set its reactive
    fields, so model/provider/usage stayed blank for the whole session —
    the TUI's status bar existed but never actually showed anything.
    """
    app = _HarnessApp(_NeverCallMeClient())
    async with app.run_test():
        status_bar = app.query_one(StatusBar)
        assert status_bar.model == "claude-sonnet-5"
        assert status_bar.provider == "anthropic"


class _UsageFakeClient:
    """One turn: two usage_delta chunks, a reply, then turn_done."""

    async def get_history(self, thread_id, workdir):
        return []

    async def send_message(self, thread_id, content, workdir):
        yield {
            "event": "usage_delta",
            "usage_metadata": {"input_tokens": 100, "output_tokens": 0, "total_tokens": 100},
        }
        yield {"event": "text_delta", "text": "hi"}
        yield {
            "event": "usage_delta",
            "usage_metadata": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        }
        yield {"event": "turn_done"}


async def test_usage_delta_events_update_the_status_bar_once_the_turn_completes():
    """usage_delta chunks accumulate into the turn's totals, which land in
    the status bar exactly once — on turn_done — mirroring the old REPL's
    own cadence (session.py only ever printed its indicator after a turn
    fully finished, never mid-stream).
    """
    app = _HarnessApp(_UsageFakeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        chat.thread_id = "t1"
        status_bar = app.query_one(StatusBar)
        assert status_bar.usage_summary == ""

        inp = chat.query_one("#chat-input", Input)
        inp.value = "hello"
        await chat.on_input_submitted(Input.Submitted(inp, "hello"))

        assert chat._session_usage.totals == (100, 20, 220)
        assert "ctx" in status_bar.usage_summary
        assert "220" in status_bar.usage_summary  # session total, from indicator_line


class _ToolThenTextClient:
    async def get_history(self, thread_id, workdir):
        return []

    async def send_message(self, thread_id, content, workdir):
        yield {"event": "text_delta", "text": "Сейчас поищу."}
        yield {
            "event": "tool_started",
            "call_id": "c1",
            "name": "web_search",
            "args": {},
            "args_preview": "погода",
        }
        yield {
            "event": "tool_finished",
            "call_id": "c1",
            "name": "web_search",
            "ok": True,
            "detail": "8 результатов",
        }
        yield {"event": "text_delta", "text": "Завтра +12."}
        yield {"event": "turn_done"}


async def test_tool_row_sits_between_the_text_before_and_after_it():
    from luna.tui.chat import LunaMessage
    from luna.tui.tool_row import ToolRow

    app = _HarnessApp(_ToolThenTextClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "погода?"))
        transcript = chat.query_one("#transcript", VerticalScroll)
        kinds = [
            type(w).__name__
            for w in transcript.children
            if isinstance(w, (UserMessage, LunaMessage, ToolRow))
        ]
        assert kinds == ["UserMessage", "LunaMessage", "ToolRow", "LunaMessage"]
        assert transcript.query_one(ToolRow).is_finished


class _DiesMidToolClient:
    async def get_history(self, thread_id, workdir):
        return []

    async def send_message(self, thread_id, content, workdir):
        yield {
            "event": "tool_started",
            "call_id": "c1",
            "name": "execute",
            "args": {},
            "args_preview": "sleep 99",
        }
        raise RuntimeError("connection dropped mid-tool")


async def test_a_turn_that_dies_mid_tool_stops_the_pulse():
    from luna.tui.tool_row import ToolRow
    from luna.tui.widgets import PulseGlyph

    app = _HarnessApp(_DiesMidToolClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        try:
            await chat.on_input_submitted(Input.Submitted(inp, "run it"))
        except RuntimeError:
            pass
        row = chat.query_one(ToolRow)
        assert row.is_finished
        assert len(row.query(PulseGlyph)) == 0


def test_render_history_builds_tool_rows():
    from luna.tui.chat import render_history
    from luna.tui.tool_row import ToolRow

    widgets = render_history(
        [
            {"role": "human", "content": "hi"},
            {"role": "tool", "name": "ls", "args_preview": "/", "ok": True, "detail": ""},
            {"role": "ai", "content": "done"},
        ]
    )
    assert isinstance(widgets[1], ToolRow)


class _ApprovalClient:
    """First stream pauses for approval; records which thread each call used."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_history(self, thread_id, workdir):
        return []

    async def send_message(self, thread_id, content, workdir):
        self.calls.append(("send", thread_id))
        yield {"event": "approval_needed", "value": {"action_requests": [{"name": "execute"}]}}

    async def approve(self, thread_id, decision, workdir):
        self.calls.append(("approve", thread_id))
        yield {"event": "turn_done"}


async def test_approval_resumes_the_thread_the_turn_started_on():
    """Regression: switching sessions while a turn waited for approval sent
    the decision to the *new* thread, leaving the original paused forever."""
    client = _ApprovalClient()
    app = _HarnessApp(client)
    async with app.run_test():
        chat = app.query_one(ChatPane)

        class _Decision:
            async def wait(self):
                chat.thread_id = "some-other-session"  # user switched meanwhile
                return {"type": "approve"}

        chat._await_approval_decision = lambda _req: _Decision()
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "run it"))
    assert client.calls == [("send", "t1"), ("approve", "t1")]
