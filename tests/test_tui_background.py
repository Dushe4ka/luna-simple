from textual import events
from textual.containers import VerticalScroll

from luna.tui.app import LunaApp
from luna.tui.chat import ChatPane, NoticeRow
from luna.tui.sidebar_sessions import SessionsSidebar
from luna.tui.status_bar import StatusBar, format_status_line
from tests.tui_fakes import FakeLunaClient

DONE = {"event": "turn_done", "usage": {}, "cost": None, "stop_reason": "end"}


def make_app(client, workdir="/w"):
    app = LunaApp(base_url="http://x", token="t", workdir=workdir, thread_id="s1")
    app.client = client
    return app


def _approval(call_id="c1", command="ls"):
    return {
        "event": "approval_needed",
        "value": {
            "action_requests": [
                {
                    "call_id": call_id,
                    "name": "execute",
                    "args": {"command": command},
                    "description": command,
                    "suggested_rule": f"execute:{command} *",
                }
            ]
        },
    }


def notices(app) -> list[str]:
    return [r.text for r in app.query_one(ChatPane).query(NoticeRow)]


async def test_approval_answers_through_decide_and_stream_continues():
    client = FakeLunaClient(turns=[[_approval(), {"event": "text_delta", "text": "done"}, DONE]])
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.click("#chat-input")
        await pilot.press(*"run ls", "enter")
        await pilot.pause(0.3)
        await pilot.click("#approve-button")
        await pilot.pause(0.3)
        text = [w.source for w in app.query("LunaMessage")]
        assert text == ["done"]
    assert client.decisions == [("c1", {"type": "approve"})]


async def test_escape_cancels_a_running_turn():
    client = FakeLunaClient(
        turns=[
            [
                {"event": "text_delta", "text": "working"},
                {"event": "_pause", "seconds": 1.0},
                {"event": "cancelled"},
            ]
        ]
    )
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.click("#chat-input")
        await pilot.press(*"long job", "enter")
        await pilot.pause(0.2)
        await pilot.press("escape")
        await pilot.pause(1.2)
        assert "прервано" in notices(app)
    assert client.cancelled == ["s1"]


async def test_ctrl_c_cancels_a_running_turn_and_only_hints_when_idle():
    client = FakeLunaClient(turns=[[{"event": "_pause", "seconds": 0.6}, {"event": "cancelled"}]])
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.click("#chat-input")
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert client.cancelled == []
        assert any("выйти" in str(n.message) for n in app._notifications)
        await pilot.press(*"long job", "enter")
        await pilot.pause(0.2)
        await pilot.press("ctrl+c")
        await pilot.pause(0.6)
    assert client.cancelled == ["s1"]
    assert app.return_code is None


async def test_ctrl_g_sends_to_background_and_status_shows_undo_depth():
    client = FakeLunaClient()
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause(0.2)
        assert app.query_one(StatusBar).undo_depth == 2
        await pilot.click("#chat-input")
        await pilot.press(*"index the repo", "ctrl+g")
        await pilot.pause(0.2)
        assert app.query_one("#chat-input").value == ""
    assert client.background_calls == ["index the repo"] and client.calls == []


async def test_background_command_runs_the_prompt_in_the_background():
    client = FakeLunaClient()
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await app.query_one(ChatPane).submit("/background index the repo")
        await pilot.pause()
    assert client.background_calls == ["index the repo"] and client.calls == []


async def test_finished_background_run_is_announced():
    client = FakeLunaClient(
        agents={
            "runs": [{"session_id": "s2", "label": "индексация", "status": "running", "cost": 0.0}],
            "tasks": [],
        }
    )
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause(0.2)
        await app.poll_agents()
        client.agents_rows = {
            "runs": [{"session_id": "s2", "label": "индексация", "status": "done", "cost": 0.01}],
            "tasks": [],
        }
        await app.poll_agents()
        await pilot.pause(0.1)
        assert any("индексация" in str(n.message) for n in app._notifications)


async def test_a_finished_background_task_is_announced_once():
    task = {"handle": "h1", "label": "тесты", "session_id": "s1", "status": "running"}
    client = FakeLunaClient(agents={"runs": [], "tasks": [task]})
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await app.poll_agents()
        client.agents_rows = {"runs": [], "tasks": [{**task, "status": "done"}]}
        await app.poll_agents()
        await app.poll_agents()
        await pilot.pause(0.1)
        hits = [n for n in app._notifications if "тесты" in str(n.message)]
        assert len(hits) == 1


async def test_pasting_an_image_path_inserts_a_mention(tmp_path):
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    client = FakeLunaClient()
    app = LunaApp(base_url="http://x", token="t", workdir=str(tmp_path), thread_id="s1")
    app.client = client
    async with app.run_test(size=(130, 36)) as pilot:
        chat_input = app.query_one("#chat-input")
        chat_input.focus()
        chat_input.post_message(events.Paste(str(tmp_path / "shot.png")))
        await pilot.pause(0.1)
        assert chat_input.value == "@shot.png "


async def test_pasting_plain_text_still_pastes_it(tmp_path):
    app = make_app(FakeLunaClient(), workdir=str(tmp_path))
    async with app.run_test(size=(130, 36)) as pilot:
        chat_input = app.query_one("#chat-input")
        chat_input.focus()
        chat_input.post_message(events.Paste("hello"))
        await pilot.pause(0.1)
        assert chat_input.value == "hello"


async def test_thinking_todos_compacted_events_render():
    turn = [
        {"event": "thinking_delta", "text": "first idea\nsecond "},
        {"event": "thinking_delta", "text": "idea"},
        {"event": "_pause", "seconds": 0.3},
        {
            "event": "todos",
            "todos": [
                {"content": "read", "status": "completed"},
                {"content": "write", "status": "in_progress"},
                {"content": "test", "status": "pending"},
            ],
        },
        {"event": "compacted", "summary": "s", "removed": 7},
        {"event": "text_delta", "text": "ok"},
        DONE,
    ]
    client = FakeLunaClient(turns=[turn])
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        chat = app.query_one(ChatPane)
        chat.run_worker(chat.submit("go"))
        await pilot.pause(0.15)
        thought = chat.query(".thought-line")
        assert len(thought) == 1
        assert str(thought.first().render()) == "second idea"
        await pilot.pause(0.4)
        assert not chat.query(".thought-line")
        assert "✓ read\n▸ write\n· test" in notices(app)
        assert "история сжата: 7 сообщений" in notices(app)


async def test_out_of_turn_approval_in_a_turn_stream_is_answered_and_stream_continues():
    out_of_turn = _approval("child:c9", "make")
    turn = [{"event": "text_delta", "text": "a", "index": 1}, out_of_turn]
    turn += [{"event": "text_delta", "text": "b", "index": 2}, DONE]
    client = FakeLunaClient(turns=[turn])
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        chat = app.query_one(ChatPane)
        chat.run_worker(chat.submit("go"))
        await pilot.pause(0.3)
        await pilot.click("#reject-button")
        await pilot.pause(0.3)
        assert [w.source for w in app.query("LunaMessage")] == ["ab"]
    assert client.decisions == [("child:c9", {"type": "reject", "message": "rejected in TUI"})]


async def test_idle_out_of_turn_approval_is_picked_up_by_polling():
    task = {"handle": "h1", "label": "sub", "session_id": "s1", "status": "running"}
    client = FakeLunaClient(
        agents={"runs": [], "tasks": [task]}, attach_events=[[_approval("child:c1", "make")]]
    )
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause(0.2)
        await app.poll_agents()
        await pilot.pause(0.6)
        await pilot.click("#approve-button")
        await pilot.pause(0.2)
    assert client.attached and client.attached[0][0] == "s1"
    assert client.decisions == [("child:c1", {"type": "approve"})]


async def test_an_already_answered_approval_in_the_replay_is_not_asked_again():
    run = {"session_id": "s1", "label": "bg", "status": "waiting", "cost": 0.0}
    replay = [
        {**_approval("c1"), "index": 0},
        {"event": "tool_started", "call_id": "c1", "name": "execute", "args": {}, "index": 1},
        {**_approval("c2", "pwd"), "index": 2},
    ]
    client = FakeLunaClient(agents={"runs": [run], "tasks": []}, attach_events=[replay])
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await app.poll_agents()
        await pilot.pause(0.6)
        await pilot.click("#approve-button")
        await pilot.pause(0.3)
        assert not any(type(s).__name__ == "ApprovalModal" for s in app.screen_stack)
    assert client.decisions == [("c2", {"type": "approve"})]


async def test_fork_effect_switches_the_pane_to_the_new_session():
    client = FakeLunaClient()
    client.command_results["/fork"] = {
        "text": "",
        "notices": [{"level": "ok", "text": "forked"}],
        "choice": None,
        "confirm": None,
        "prompt": None,
        "effects": {"session": "s-fork"},
    }
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input")
        inp.focus()
        await pilot.press(*"/fork", "enter", "enter")
        await pilot.pause(0.3)
        assert chat.thread_id == "s-fork"
        assert app.query_one(SessionsSidebar).current_thread_id == "s-fork"


async def test_sidebar_marks_running_and_background_sessions():
    rows = [
        {
            "id": "s1",
            "title": "here",
            "updated": 0,
            "group": "today",
            "relative_time": "сейчас",
            "running": True,
            "background": False,
        },
        {
            "id": "s2",
            "title": "away",
            "updated": 0,
            "group": "today",
            "relative_time": "1м",
            "running": True,
            "background": True,
        },
        {
            "id": "s3",
            "title": "idle",
            "updated": 0,
            "group": "today",
            "relative_time": "2м",
            "running": False,
            "background": False,
        },
    ]
    app = make_app(FakeLunaClient(sessions=rows))
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause(0.1)
        sidebar = app.query_one(SessionsSidebar)
        items = list(sidebar.query(".session-item"))
        assert [i.data_thread_id for i in items] == ["s1", "s2", "s3"]
        markers = [[str(m.render()) for m in i.query(".session-marker")] for i in items]
        assert markers == [["● "], ["◌ "], []]
        assert items[0].query_one(".session-marker").has_class("session-running")
        assert items[1].query_one(".session-marker").has_class("session-background")


def test_status_line_shows_undo_only_when_there_is_something_to_undo():
    base = dict(model="m", cost_usd=0, context_file=None, plan_mode=False)
    assert "undo" not in format_status_line(**base, undo_depth=0)
    assert format_status_line(**base, undo_depth=2, pinned=1).endswith("pinned: 1 · undo: 2")


async def test_closing_the_app_mid_turn_closes_the_stream():
    closed = []

    class _Client(FakeLunaClient):
        async def send(self, session_id, text, workdir, *, images=(), reload_after=False):
            try:
                yield {"event": "text_delta", "text": "x"}
                import asyncio

                await asyncio.sleep(30)
            finally:
                closed.append(True)

    app = make_app(_Client())
    async with app.run_test(size=(130, 36)) as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input")
        inp.focus()
        await pilot.press(*"go", "enter")
        await pilot.pause(0.2)
        assert chat.busy
        assert app.query_one(ChatPane).query_one("#transcript", VerticalScroll)
    assert closed == [True]


def test_compacted_notice_uses_the_right_plural():
    from luna.tui.chat import _messages_word

    assert [_messages_word(n) for n in (1, 3, 7, 11, 21, 22, 112)] == [
        "сообщение",
        "сообщения",
        "сообщений",
        "сообщений",
        "сообщение",
        "сообщения",
        "сообщений",
    ]


async def test_a_failed_decide_lets_the_same_approval_be_asked_again():
    from luna_core.client import ServerError

    class _Flaky(FakeLunaClient):
        async def decide(self, session_id, call_id, decision):
            self.decisions.append((call_id, decision))
            if len(self.decisions) == 1:
                raise ServerError("HTTP 500: boom")

    client = _Flaky()
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        chat = app.query_one(ChatPane)
        value = _approval("c1")["value"]
        chat.run_worker(chat._answer("s1", value))
        await pilot.pause(0.2)
        await pilot.click("#approve-button")
        await pilot.pause(0.2)
        assert "c1" not in chat._asked
        chat.run_worker(chat._answer("s1", value))
        await pilot.pause(0.2)
        await pilot.click("#approve-button")
        await pilot.pause(0.2)
    assert [d[0] for d in client.decisions] == ["c1", "c1"]


async def test_pasting_a_path_of_an_unknown_user_home_is_pasted_as_text(tmp_path):
    app = make_app(FakeLunaClient(), workdir=str(tmp_path))
    async with app.run_test(size=(130, 36)) as pilot:
        chat_input = app.query_one("#chat-input")
        chat_input.focus()
        chat_input.post_message(events.Paste("~nosuchuser_luna/x.png"))
        await pilot.pause(0.1)
        assert chat_input.value == "~nosuchuser_luna/x.png"


class _HistoryClient(FakeLunaClient):
    """A fake whose history grows once the background run is over."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.history_rows: list[dict] = []
        self.history_calls = 0

    async def history(self, session_id, workdir):
        self.history_calls += 1
        return list(self.history_rows)


def _run(status, session_id="s1"):
    return {"session_id": session_id, "label": "bg", "status": status, "cost": 0.0}


async def test_finished_background_run_of_the_open_session_reloads_its_history():
    client = _HistoryClient(agents={"runs": [_run("running")], "tasks": []})
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause(0.2)
        await app.poll_agents()
        before = client.history_calls
        client.history_rows = [{"role": "assistant", "content": "результат"}]
        client.agents_rows = {"runs": [_run("done")], "tasks": []}
        await app.poll_agents()
        await pilot.pause(0.2)
        assert client.history_calls > before
        assert [w.source for w in app.query("LunaMessage")] == ["результат"]


async def test_history_reload_waits_for_an_idle_pane():
    client = _HistoryClient(agents={"runs": [_run("running")], "tasks": []})
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause(0.2)
        await app.poll_agents()
        chat = app.query_one(ChatPane)
        chat.busy = True
        before = client.history_calls
        client.agents_rows = {"runs": [_run("done")], "tasks": []}
        await app.poll_agents()
        assert client.history_calls == before  # a streaming turn is not clobbered
        chat.busy = False
        await app.poll_agents()
        assert client.history_calls == before + 1


async def test_a_run_of_another_session_does_not_reload_the_open_history():
    client = _HistoryClient(agents={"runs": [_run("running", "s2")], "tasks": []})
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause(0.2)
        await app.poll_agents()
        before = client.history_calls
        client.agents_rows = {"runs": [_run("done", "s2")], "tasks": []}
        await app.poll_agents()
        assert client.history_calls == before


async def test_a_run_that_vanishes_from_agents_is_finished_and_polling_stops(monkeypatch):
    client = _HistoryClient(agents={"runs": [_run("running")], "tasks": []})
    app = make_app(client)
    toasts = []
    async with app.run_test(size=(130, 36)) as pilot:
        monkeypatch.setattr(app, "notify", lambda text, **kw: toasts.append(text))
        await pilot.pause(0.2)
        await app.poll_agents()
        client.agents_rows = {"runs": [], "tasks": []}
        await app.poll_agents()
        await app.poll_agents()
        assert toasts == ["Фоновая сессия «bg» завершена"]
        assert not any(s in app._LIVE for s in app._runs.values())


async def test_idle_watcher_is_not_reattached_on_every_poll(monkeypatch):
    task = {"handle": "h1", "label": "sub", "session_id": "s1", "status": "running"}
    client = FakeLunaClient(agents={"runs": [], "tasks": [task]})  # attach() ends at once
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause(0.2)
        for _ in range(4):
            await app.poll_agents()
            await pilot.pause(0.1)
        assert len(client.attached) == 1


async def test_idle_watcher_reattaches_after_a_backoff(monkeypatch):
    monkeypatch.setattr(ChatPane, "WATCH_BACKOFF", (0.1, 0.1))
    task = {"handle": "h1", "label": "sub", "session_id": "s1", "status": "running"}
    client = FakeLunaClient(agents={"runs": [], "tasks": [task]})
    app = make_app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause(0.2)
        await app.poll_agents()
        await pilot.pause(0.1)
        await app.poll_agents()  # keeps the watch wanted
        await pilot.pause(0.5)
        assert len(client.attached) >= 2
