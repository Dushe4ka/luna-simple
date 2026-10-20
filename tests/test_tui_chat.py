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

    ``#transcript`` holds one message widget per turn (UserMessage /
    LunaMessage / SystemMessage) instead of one shared Markdown widget, so
    a plain ``.source`` lookup no longer exists on the container itself.
    """
    widgets = transcript.query("UserMessage, LunaMessage, SystemMessage")
    return "\n".join(child.source for child in widgets)


def test_render_history_shows_a_completion_notice_as_a_notice_row():
    """A background run that finished while the TUI was closed (server ``notice`` entry)."""
    from luna.tui.chat import NoticeRow

    widgets = render_history(
        [{"role": "notice", "level": "ok", "text": "фоновая сессия «go» завершена"}]
    )
    assert isinstance(widgets[0], NoticeRow)
    assert (widgets[0].level, widgets[0].text) == ("ok", "фоновая сессия «go» завершена")


async def test_render_history_labels_human_and_ai_turns_you_then_luna():
    """render_history builds one widget per turn — Markdown only parses its
    ``.source`` once mounted, so this must check the widgets live, inside a
    running app, rather than the pre-refactor plain-string return value.
    """
    widgets = render_history(
        [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "done"},
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
        assert widgets[0].source == "fix the bug"
        assert "**Luna**" not in widgets[1].source


def test_filter_commands_matches_prefix():
    results = filter_commands("/cle")
    names = [r[0] for r in results]
    assert all(n.startswith("/cle") for n in names)
    assert len(results) >= 1


def test_filter_commands_empty_slash_returns_all():
    from luna.tui.commands import FALLBACK_COMMANDS

    results = filter_commands("/")
    assert len(results) == len(FALLBACK_COMMANDS)
    assert {"/background", "/btw", "/steer", "/rewind", "/export", "/fork"} <= {
        name for name, _ in results
    }


def test_filter_commands_no_match_returns_empty():
    assert filter_commands("/zzz-nope") == []


class _BaseFakeClient:
    """Defaults for the server calls a ChatPane makes besides the turn itself."""

    async def history(self, thread_id, workdir):
        return []

    async def state(self, thread_id, workdir):
        return {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "plan": False,
            "pinned": [],
            "usage_summary": "",
        }

    async def commands(self, workdir):
        from luna.tui.commands import FALLBACK_COMMANDS

        return FALLBACK_COMMANDS

    async def run_command(self, thread_id, line, workdir):
        raise AssertionError(f"unexpected command {line!r}")


class _FakeClient(_BaseFakeClient):
    """Sends one text_delta then raises, mimicking a dropped SSE connection."""

    async def history(self, thread_id, workdir):
        return []

    async def send(self, thread_id, content, workdir, *, images=(), reload_after=False):
        yield {"event": "text_delta", "text": "partial "}
        raise RuntimeError("connection dropped mid-stream")


class _HarnessApp(App):
    """Minimal app hosting a ChatPane, with a fake server client attached."""

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client

    def compose(self) -> ComposeResult:
        yield ChatPane(workdir=".", thread_id="t1", provider="anthropic", model="claude-sonnet-5")


class _NeverCallMeClient(_BaseFakeClient):
    """Fails the test the instant `send` is invoked."""

    async def history(self, thread_id, workdir):
        return []

    async def send(self, thread_id, content, workdir, *, images=(), reload_after=False):
        raise AssertionError(f"a slash command must never be sent to the agent: {content!r}")
        yield  # pragma: no cover - unreachable, keeps this an async generator


async def test_quit_exits_the_app_without_ever_asking_the_agent():
    """Regression: reproduces a real incident where submitting `/quit`
    made the model role-play a reply ("Пока! Обращайся, когда
    понадобится.") instead of actually exiting — every "/" line was sent
    to the agent like any other message. `/quit` (and `/exit`) must close
    the app and must never reach `send`.
    """
    app = _HarnessApp(_NeverCallMeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        inp.value = "/quit"
        await chat.submit("/quit")
        assert app._exit is True


async def test_clear_command_empties_the_transcript_locally():
    app = _HarnessApp(_NeverCallMeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        transcript = chat.query_one("#transcript", VerticalScroll)
        await transcript.mount(UserMessage("hello"))
        assert "hello" in transcript_text(transcript)

        inp = chat.query_one("#chat-input", Input)
        inp.value = "/clear"
        await chat.submit("/clear")

        assert list(transcript.children) == []


async def test_help_command_lists_commands_without_asking_the_agent():
    app = _HarnessApp(_NeverCallMeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        inp.value = "/help"
        await chat.submit("/help")

        transcript = chat.query_one("#transcript", VerticalScroll)
        text = transcript_text(transcript)
        assert "**Команды**" in text
        assert "/clear" in text
        assert "/undo" in text


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
                await chat.submit("hello")
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
            await chat.submit("hello there")

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
        from luna.tui.commands import FALLBACK_COMMANDS

        expected_rows = min(len(FALLBACK_COMMANDS), 10)
        # border-box: the dropdown's 2-row round border comes on top of the rows
        assert dropdown.region.height == expected_rows + 2


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


class _ErrorFakeClient(_BaseFakeClient):
    """A turn that fails server-side: one text_delta, then an error event."""

    async def history(self, thread_id, workdir):
        return []

    async def send(self, thread_id, content, workdir, *, images=(), reload_after=False):
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

        await chat.submit("hello")

        transcript = chat.query_one("#transcript", VerticalScroll)
        assert "insufficient balance" in transcript_text(transcript)


def _approval(call_id, path):
    return {
        "event": "approval_needed",
        "value": {
            "action_requests": [
                {
                    "call_id": call_id,
                    "name": "write_file",
                    "args": {"file_path": path, "content": "x"},
                    "description": path,
                    "suggested_rule": f"write_file:{path}",
                }
            ]
        },
    }


class _ApprovalFakeClient(_BaseFakeClient):
    """One stream: text, an approval_needed, then (after decide) more text."""

    def __init__(self) -> None:
        self.decide_calls: list[tuple[str, str, dict]] = []

    async def history(self, thread_id, workdir):
        return []

    async def send(self, thread_id, content, workdir, *, images=(), reload_after=False):
        yield {"event": "text_delta", "text": "before "}
        yield _approval("c1", "/a.py")
        assert self.decide_calls, "the stream continues only after the decision"
        yield {"event": "text_delta", "text": "after "}

    async def decide(self, thread_id, call_id, decision):
        self.decide_calls.append((thread_id, call_id, decision))


async def test_approval_needed_pushes_modal_and_the_same_stream_continues():
    """The approval_needed branch pushes ApprovalModal, answers via decide(),
    and keeps reading the same stream — all inside one try/finally
    (stream.stop() still runs once).
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
            task = asyncio.create_task(chat.submit("hello"))
            await pilot.pause()
            await pilot.click("#approve-button")
            await asyncio.wait_for(task, timeout=5)
        finally:
            MarkdownStream.stop = original_stop

        assert len(stop_calls) == 1
        assert client.decide_calls == [("t1", "c1", {"type": "approve"})]


class _TwoApprovalFakeClient(_BaseFakeClient):
    """A turn that pauses for approval TWICE in one stream before finishing."""

    def __init__(self) -> None:
        self.decide_calls: list[tuple[str, str, dict]] = []

    async def history(self, thread_id, workdir):
        return []

    async def send(self, thread_id, content, workdir, *, images=(), reload_after=False):
        yield {"event": "text_delta", "text": "first "}
        yield _approval("c1", "/one.py")
        yield {"event": "text_delta", "text": "second "}
        yield _approval("c2", "/two.py")
        yield {"event": "text_delta", "text": "done"}

    async def decide(self, thread_id, call_id, decision):
        self.decide_calls.append((thread_id, call_id, decision))


async def test_two_sequential_approvals_in_one_turn_both_get_answered():
    """Regression (C2): a turn needing two approvals must not be stranded."""
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
            task = asyncio.create_task(chat.submit("hello"))
            await pilot.pause()
            await pilot.click("#approve-button")  # first approval
            await pilot.pause()
            await pilot.click("#approve-button")  # second approval
            await asyncio.wait_for(task, timeout=5)
        finally:
            MarkdownStream.stop = original_stop
            MarkdownStream.write = original_write

    assert client.decide_calls == [
        ("t1", "c1", {"type": "approve"}),
        ("t1", "c2", {"type": "approve"}),
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


class _UsageFakeClient(_BaseFakeClient):
    def __init__(self) -> None:
        self.turns = 0

    async def state(self, thread_id, workdir):
        state = await super().state(thread_id, workdir)
        state["usage_summary"] = "ctx ~220/200k" if self.turns else ""
        return state

    """One turn: two usage_delta chunks, a reply, then turn_done."""

    async def history(self, thread_id, workdir):
        return []

    async def send(self, thread_id, content, workdir, *, images=(), reload_after=False):
        yield {
            "event": "usage_delta",
            "usage_metadata": {"input_tokens": 100, "output_tokens": 0, "total_tokens": 100},
        }
        yield {"event": "text_delta", "text": "hi"}
        yield {
            "event": "usage_delta",
            "usage_metadata": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        }
        self.turns += 1
        yield {"event": "turn_done"}


async def test_usage_delta_events_update_the_status_bar_once_the_turn_completes():
    """The status bar's usage comes from the server's session state and is
    refreshed exactly once — on turn_done — mirroring the old REPL's own
    cadence (session.py only ever printed its indicator after a turn fully
    finished, never mid-stream).
    """
    app = _HarnessApp(_UsageFakeClient())
    async with app.run_test():
        chat = app.query_one(ChatPane)
        chat.thread_id = "t1"
        status_bar = app.query_one(StatusBar)
        assert status_bar.usage_summary == ""

        inp = chat.query_one("#chat-input", Input)
        inp.value = "hello"
        await chat.submit("hello")

        # the server owns usage now: the bar shows its summary after turn_done
        assert status_bar.usage_summary == "ctx ~220/200k"


class _ToolThenTextClient(_BaseFakeClient):
    async def history(self, thread_id, workdir):
        return []

    async def send(self, thread_id, content, workdir, *, images=(), reload_after=False):
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
        chat.query_one("#chat-input", Input)
        await chat.submit("погода?")
        transcript = chat.query_one("#transcript", VerticalScroll)
        kinds = [
            type(w).__name__
            for w in transcript.children
            if isinstance(w, (UserMessage, LunaMessage, ToolRow))
        ]
        assert kinds == ["UserMessage", "LunaMessage", "ToolRow", "LunaMessage"]
        assert transcript.query_one(ToolRow).is_finished


class _DiesMidToolClient(_BaseFakeClient):
    async def history(self, thread_id, workdir):
        return []

    async def send(self, thread_id, content, workdir, *, images=(), reload_after=False):
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
        chat.query_one("#chat-input", Input)
        try:
            await chat.submit("run it")
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
            {"role": "user", "content": "hi"},
            {"role": "tool", "name": "ls", "args_preview": "/", "ok": True, "detail": ""},
            {"role": "assistant", "content": "done"},
        ]
    )
    assert isinstance(widgets[1], ToolRow)


class _ApprovalClient(_BaseFakeClient):
    """The stream pauses for approval; records which session each call used."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def history(self, thread_id, workdir):
        return []

    async def send(self, thread_id, content, workdir, *, images=(), reload_after=False):
        self.calls.append(("send", thread_id))
        yield {
            "event": "approval_needed",
            "value": {"action_requests": [{"call_id": "c1", "name": "execute", "args": {}}]},
        }
        yield {"event": "turn_done"}

    async def decide(self, thread_id, call_id, decision):
        self.calls.append(("decide", thread_id))


async def test_approval_is_decided_on_the_session_the_turn_started_on():
    """Regression: switching sessions while a turn waited for approval sent
    the decision to the *new* session, leaving the original paused forever."""
    client = _ApprovalClient()
    app = _HarnessApp(client)
    async with app.run_test():
        chat = app.query_one(ChatPane)

        class _Decision:
            async def wait(self):
                chat.thread_id = "some-other-session"  # user switched meanwhile
                return {"type": "approve"}

        chat._await_approval_decision = lambda _req: _Decision()
        chat.query_one("#chat-input", Input)
        await chat.submit("run it")
    assert client.calls == [("send", "t1"), ("decide", "t1")]


async def test_user_message_is_literal_text_with_a_peri_marker():
    """Regression: user text went through Markdown, so `__init__` became a
    bold "init" and `# x` a heading; the spec asks for a bold $peri "›"."""

    from luna.tui.chat import user_line
    from luna.tui.theme import TUI_VARIABLES

    msg = UserMessage("fix __init__ and # header")
    assert not isinstance(msg, Markdown)
    assert msg.source == "fix __init__ and # header"
    line = user_line("fix __init__")
    assert line.plain == "› fix __init__"
    marker = line.spans[0]
    assert (marker.start, marker.end) == (0, 2)
    assert TUI_VARIABLES["peri"] in str(marker.style) and "bold" in str(marker.style)


async def test_quit_alias_still_exits_although_it_is_not_listed():
    app = _HarnessApp(_NeverCallMeClient())
    async with app.run_test():
        await app.query_one(ChatPane).submit("/quit")
        assert app._exit is True
