from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from luna.tui.chat import ChatPane, NoticeRow
from luna.tui.pickers import ChoiceModal, ConfirmModal


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
