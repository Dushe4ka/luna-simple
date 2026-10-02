import asyncio

import httpx
import pytest
from langchain_core.messages import AIMessage

from luna.server.app import create_app
from luna.server.run import make_session_agent_factory


@pytest.fixture
def api(tmp_path):
    from tests.conftest import FakeToolCallingModel

    app = create_app(
        token="t",
        session_agent_factory=make_session_agent_factory(
            model=FakeToolCallingModel(responses=[AIMessage(content="ok")])
        ),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer t"},
    )
    return client, app, str(tmp_path)


async def _cmd(c, wd, line, thread="t1"):
    return await c.post(f"/sessions/{thread}/command", json={"workdir": wd, "line": line})


async def test_plan_command_changes_only_that_session(api):
    c, app, wd = api
    async with c:
        resp = await _cmd(c, wd, "/plan")
        state_one = (await c.get("/sessions/t1/state", params={"workdir": wd})).json()
        state_two = (await c.get("/sessions/t2/state", params={"workdir": wd})).json()
    assert resp.json()["notices"] == [{"level": "info", "text": "plan mode: on"}]
    assert state_one["plan"] is True and state_two["plan"] is False


async def test_confirm_round_trip_for_undo(api, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_is_git", lambda wd: False)
    monkeypatch.setattr(builtin, "peek_last", lambda wd, sid: "undo x")
    monkeypatch.setattr(builtin, "undo_last", lambda wd, sid: "reverted x")
    c, _, wd = api
    async with c:
        first = (await _cmd(c, wd, "/undo")).json()
        second = (await _cmd(c, wd, first["confirm"]["resubmit"])).json()
    assert first["confirm"]["question"] == "undo x?"
    assert second["notices"] == [{"level": "info", "text": "reverted x"}]


async def test_ui_commands_are_refused_by_the_server(api):
    c, _, wd = api
    async with c:
        resp = await _cmd(c, wd, "/help")
    assert resp.status_code == 400 and resp.json() == {"error": "client_command"}


async def test_mutate_is_409_while_streaming_but_read_is_not(api):
    c, app, wd = api
    async with c:
        runtime = app.state.runtimes.get("t1", wd)
        await runtime.lock.acquire()
        try:
            busy = await _cmd(c, wd, "/plan")
            read = await _cmd(c, wd, "/context")
        finally:
            runtime.lock.release()
    assert busy.status_code == 409
    assert read.status_code == 200


async def test_read_command_answers_while_a_turn_streams(api, monkeypatch):
    """The sync graph must not block the event loop: /usage answers mid-turn."""
    import time

    from luna.server import turns

    started = asyncio.Event()
    real = turns._graph_events

    def slow_graph(*a, **k):
        started_loop.call_soon_threadsafe(started.set)
        time.sleep(1.0)
        yield from real(*a, **k)

    monkeypatch.setattr(turns, "_graph_events", slow_graph)
    c, _, wd = api
    started_loop = asyncio.get_running_loop()
    async with c:
        turn = asyncio.create_task(
            c.post("/sessions/t1/messages", json={"workdir": wd, "content": "hi"})
        )
        await asyncio.wait_for(started.wait(), 5)
        t0 = time.monotonic()
        resp = await _cmd(c, wd, "/usage")
        elapsed = time.monotonic() - t0
        await turn
    assert resp.status_code == 200 and elapsed < 0.8


async def test_commands_list_includes_user_commands(api, tmp_path):
    (tmp_path / ".luna" / "commands").mkdir(parents=True)
    (tmp_path / ".luna" / "commands" / "ship.md").write_text(
        "---\ndescription: ship it\n---\nship $ARGUMENTS\n"
    )
    c, _, wd = api
    async with c:
        cmds = (await c.get("/commands", params={"workdir": wd})).json()["commands"]
    assert {"name": "/ship", "help": "ship it", "kind": "prompt"} in cmds
    assert any(x["name"] == "/model" and x["kind"] == "mutate" for x in cmds)


async def test_mutate_command_holds_the_session_lock(api, monkeypatch):
    """A long /verify must block a turn on the same thread (no parallel writes)."""
    import time

    from luna.commands import builtin

    monkeypatch.setattr(builtin, "run_verify", lambda cmd, wd: (time.sleep(0.8), (True, ""))[1])
    c, app, wd = api
    runtime = app.state.runtimes.get("t1", wd)
    runtime.state.provider = None
    async with c:
        monkeypatch.setattr(type(runtime), "config", lambda self: _cfg_with_verify(self.workdir))
        cmd = asyncio.create_task(_cmd(c, wd, "/verify"))
        await asyncio.sleep(0.3)
        turn = await c.post("/sessions/t1/messages", json={"workdir": wd, "content": "hi"})
        await cmd
    assert turn.status_code == 409


def _cfg_with_verify(workdir):
    from luna.config.config import LunaConfig

    return LunaConfig(workdir=workdir, verify_command="make check")


async def test_read_command_does_not_save_state(api, monkeypatch):
    c, app, wd = api
    runtime = app.state.runtimes.get("t1", wd)
    saves = []
    monkeypatch.setattr(runtime, "save", lambda: saves.append(1))
    async with c:
        await _cmd(c, wd, "/context")
    assert saves == []
