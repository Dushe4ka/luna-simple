from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Input

from luna.tui.chat import ChatPane, NoticeRow
from luna.tui.pickers import ChoiceModal, ConfirmModal
from luna.tui.status_bar import format_status_line


class _Client:
    def __init__(self, results):
        self.results = list(results)
        self.lines = []

    async def get_history(self, thread_id, workdir):
        return []

    async def get_state(self, thread_id, workdir):
        return {"provider": "p", "model": "m", "plan": False, "pinned": [], "usage_summary": ""}

    async def list_commands(self, workdir):
        return [
            {"name": "/model", "help": "pick", "kind": "mutate"},
            {"name": "/usage", "help": "u", "kind": "read"},
        ]

    async def run_command(self, thread_id, line, workdir):
        self.lines.append(line)
        return self.results.pop(0)


class _Host(App):
    def __init__(self, client):
        super().__init__()
        self.client = client

    def compose(self) -> ComposeResult:
        yield ChatPane(workdir="/w", thread_id="t1", provider="p", model="m")


def _res(**kw):
    base = {
        "text": "",
        "notices": [],
        "choice": None,
        "confirm": None,
        "prompt": None,
        "effects": {},
    }
    base.update(kw)
    return base


async def test_notices_and_text_render_in_the_transcript():
    client = _Client([_res(text="**hi**", notices=[{"level": "info", "text": "plan mode: on"}])])
    app = _Host(client)
    async with app.run_test():
        chat = app.query_one(ChatPane)
        await chat.submit("/plan")
        rows = chat.query_one("#transcript", VerticalScroll).query(NoticeRow)
        assert [r.text for r in rows] == ["plan mode: on"]
    assert client.lines == ["/plan"]


async def test_choice_opens_a_picker_and_resubmits():
    client = _Client(
        [
            _res(
                choice={
                    "title": "Модель",
                    "options": [["a", "a"], ["b", "b"]],
                    "resubmit": "/model {value}",
                }
            ),
            _res(notices=[{"level": "info", "text": "model → b"}]),
        ]
    )
    app = _Host(client)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        chat.run_worker(chat.submit("/model"))
        await pilot.pause()
        assert isinstance(app.screen, ChoiceModal)
        await pilot.press("down", "enter")
        await pilot.pause()
    assert client.lines == ["/model", "/model b"]


async def test_confirm_no_shows_cancelled():
    client = _Client(
        [
            _res(
                confirm={
                    "question": "undo x?",
                    "resubmit": "/undo --yes",
                    "cancelled": "undo cancelled",
                }
            )
        ]
    )
    app = _Host(client)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        chat.run_worker(chat.submit("/undo"))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("escape")
        await pilot.pause()
        rows = chat.query(NoticeRow)
        assert [r.text for r in rows] == ["undo cancelled"]
    assert client.lines == ["/undo"]


async def test_prompt_result_is_sent_as_a_message():
    sent = []

    class _PromptClient(_Client):
        async def send_message(self, thread_id, content, workdir):
            sent.append(content)
            yield {"event": "turn_done"}

    app = _Host(_PromptClient([_res(prompt="WRITE AGENTS.md")]))
    async with app.run_test():
        await app.query_one(ChatPane).submit("/init")
    assert sent == ["WRITE AGENTS.md"]


async def test_help_is_local_and_lists_server_commands():
    client = _Client([])
    app = _Host(client)
    async with app.run_test():
        chat = app.query_one(ChatPane)
        await chat.submit("/help")
    assert client.lines == []


def test_status_line_shows_pinned_count():
    line = format_status_line(
        model="m", cost_usd=0, context_file=None, plan_mode=True, undo_depth=0, pinned=2
    )
    assert "plan: on" in line and "pinned: 2" in line
    assert "pinned" not in format_status_line(
        model="m", cost_usd=0, context_file=None, plan_mode=False, undo_depth=0
    )


async def test_plan_state_colours_the_input_border():
    client = _Client([])

    async def plan_state(thread_id, workdir):
        return {
            "provider": "p",
            "model": "m",
            "plan": True,
            "pinned": ["a"],
            "usage_summary": "ctx 1k",
        }

    client.get_state = plan_state
    app = _Host(client)
    async with app.run_test():
        chat = app.query_one(ChatPane)
        await chat.refresh_state()
        assert chat.has_class("-plan")
        bar = chat.query_one("#status-bar")
        assert bar.plan_mode is True and bar.pinned == 1 and bar.usage_summary == "ctx 1k"


async def test_read_command_runs_mid_turn_but_a_message_is_refused():
    client = _Client([_res(notices=[{"level": "info", "text": "no usage recorded yet"}])])
    app = _Host(client)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        chat.busy = True
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "/usage"))
        await pilot.pause()
        await chat.on_input_submitted(Input.Submitted(inp, "hello"))
        await pilot.pause()
    assert client.lines == ["/usage"]


async def test_autocomplete_uses_the_server_command_list():
    from luna.tui.commands import filter_commands

    cmds = [
        {"name": "/ship", "help": "ship it", "kind": "prompt"},
        {"name": "/model", "help": "m", "kind": "mutate"},
    ]
    assert filter_commands("/sh", cmds) == [("/ship", "ship it")]


def test_status_line_names_the_provider_when_the_model_is_the_default():
    line = format_status_line(
        model="", provider="deepseek", cost_usd=0, context_file=None, plan_mode=False, undo_depth=0
    )
    assert line.startswith("deepseek (модель по умолчанию)")


async def test_enter_on_an_exactly_typed_command_runs_it_at_once():
    """The dropdown accepts a *partial* name; a fully typed one runs on the first Enter."""
    client = _Client([_res(text="tools…")])
    app = _Host(client)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        inp.focus()
        for ch in "/usage":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
    assert client.lines == ["/usage"]


async def test_busy_is_set_before_the_worker_starts_so_double_enter_is_refused():
    sent = []

    class _SlowClient(_Client):
        async def send_message(self, thread_id, content, workdir):
            sent.append(content)
            import asyncio

            await asyncio.sleep(0.3)
            yield {"event": "turn_done"}

    app = _Host(_SlowClient([]))
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "one"))
        assert chat.busy is True
        await chat.on_input_submitted(Input.Submitted(inp, "two"))
        await pilot.pause(0.6)
        assert chat.busy is False
    assert sent == ["one"]


async def test_an_unexpected_error_in_a_command_is_shown_not_swallowed():
    class _BrokenClient(_Client):
        async def run_command(self, thread_id, line, workdir):
            raise KeyError("text")

    app = _Host(_BrokenClient([]))
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "/plan"))
        await pilot.pause()
        texts = [r.text for r in chat.query(NoticeRow)]
        assert any("KeyError" in t for t in texts)
        assert chat.busy is False


async def test_prompt_with_reload_effect_is_sent_with_the_flag():
    calls = []

    class _PromptClient(_Client):
        async def send_message(self, thread_id, content, workdir, reload_after=False):
            calls.append((content, reload_after))
            yield {"event": "turn_done"}

    result = _res(prompt="WRITE AGENTS.md", effects={"reload_after_turn": True})
    app = _Host(_PromptClient([result]))
    async with app.run_test():
        await app.query_one(ChatPane).submit("/init")
    assert calls == [("WRITE AGENTS.md", True)]


async def test_read_command_mid_turn_shows_a_toast_not_transcript_rows():
    """Mid-turn output must not be wedged into the live answer."""
    client = _Client([_res(notices=[{"level": "info", "text": "turns: 1"}], text="cost: $0.01")])
    app = _Host(client)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        toasts = []
        chat.notify = lambda message, **kw: toasts.append((message, kw))
        transcript = chat.query_one("#transcript", VerticalScroll)
        before = len(transcript.children)
        chat.busy = True  # a turn is streaming
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "/usage"))
        await pilot.pause()
        assert len(transcript.children) == before
    assert toasts and "turns: 1" in toasts[0][0] and "cost: $0.01" in toasts[0][0]
    assert toasts[0][1].get("title") == "/usage"
    assert toasts[0][1].get("markup") is False  # diffs/test output contain [brackets]


async def test_clear_is_refused_while_a_turn_streams():
    app = _Host(_Client([]))
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        toasts = []
        chat.notify = lambda message, **kw: toasts.append(message)
        transcript = chat.query_one("#transcript", VerticalScroll)
        before = len(transcript.children)
        chat.busy = True
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "/clear"))
        await pilot.pause()
        assert len(transcript.children) == before
    assert toasts == ["Очистка недоступна во время ответа."]
