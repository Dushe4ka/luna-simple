import asyncio

import pytest
from luna_core.providers.fake import FakeProvider

from tests.server_helpers import client, make_app, turn


@pytest.fixture
def api(tmp_path):
    def make(**kwargs):
        app = make_app(**kwargs)
        return client(app), app, str(tmp_path)

    return make


async def _cmd(c, wd, line, session="t1"):
    return await c.post(f"/sessions/{session}/command", json={"workdir": wd, "line": line})


async def test_plan_command_changes_only_that_session(api):
    c, app, wd = api()
    async with c:
        resp = await _cmd(c, wd, "/plan")
        state_one = (await c.get("/sessions/t1/state", params={"workdir": wd})).json()
        state_two = (await c.get("/sessions/t2/state", params={"workdir": wd})).json()
    assert resp.json()["notices"] == [{"level": "info", "text": "plan mode: on"}]
    assert state_one["plan"] is True and state_two["plan"] is False


async def test_state_reports_usage_undo_depth_and_running(api, tmp_path):
    (tmp_path / ".luna.toml").write_text("[agent]\nyolo = true\n")
    c, app, wd = api(
        default=[
            FakeProvider.call("write_file", {"file_path": "a.txt", "content": "x"}),
            FakeProvider.text("done"),
        ]
    )
    async with c:
        before = (await c.get("/sessions/t1/state", params={"workdir": wd})).json()
        await turn(c, "t1", wd, "write")
        after = (await c.get("/sessions/t1/state", params={"workdir": wd})).json()
    assert before["usage_summary"] == "" and before["undo_depth"] == 0
    assert after["usage_summary"] == "0k in · 0k out" and after["undo_depth"] == 1
    assert after["running"] is False
    assert set(after) == {
        "provider",
        "model",
        "plan",
        "pinned",
        "usage_summary",
        "undo_depth",
        "running",
    }


async def test_confirm_round_trip_for_undo(api, tmp_path):
    (tmp_path / ".luna.toml").write_text("[agent]\nyolo = true\n")
    c, _, wd = api(
        default=[
            FakeProvider.call("write_file", {"file_path": "a.txt", "content": "x"}),
            FakeProvider.text("done"),
        ]
    )
    async with c:
        nothing = (await _cmd(c, wd, "/undo")).json()
        await turn(c, "t1", wd, "write")
        first = (await _cmd(c, wd, "/undo")).json()
        second = (await _cmd(c, wd, first["confirm"]["resubmit"])).json()
        history = (await c.get("/sessions/t1/messages", params={"workdir": wd})).json()
    assert nothing["notices"] == [{"level": "dim", "text": "nothing to undo"}]
    assert first["confirm"]["question"] == "undo the last turn «write» (files + conversation)?"
    assert second["notices"][0]["level"] == "info"
    assert not (tmp_path / "a.txt").exists() and history["messages"] == []


async def test_ui_commands_are_refused_by_the_server(api):
    c, _, wd = api()
    async with c:
        resp = await _cmd(c, wd, "/help")
    assert resp.status_code == 400 and resp.json() == {"error": "client_command"}


async def test_mutate_is_409_while_the_lock_is_held_but_read_is_not(api):
    c, app, wd = api()
    async with c:
        runtime = await app.state.runtimes.get("t1", wd)
        await runtime.lock.acquire()
        try:
            busy = await _cmd(c, wd, "/plan")
            read = await _cmd(c, wd, "/context")
        finally:
            runtime.lock.release()
    assert busy.status_code == 409
    assert read.status_code == 200


async def test_read_command_answers_while_a_turn_runs_and_mutate_is_409(api, tmp_path):
    import time

    (tmp_path / ".luna.toml").write_text("[agent]\nyolo = true\n")
    c, app, wd = api(
        default=[
            FakeProvider.call("execute", {"command": "sleep 1"}, "c1"),
            FakeProvider.text("done"),
        ]
    )
    async with c:
        info = await c.post("/sessions/t1/background", json={"workdir": wd, "content": "wait"})
        assert info.status_code == 200
        while not any(e["event"] == "tool_started" for e in app.state.manager.events("t1")):
            await asyncio.sleep(0.02)
        t0 = time.monotonic()
        read = await _cmd(c, wd, "/usage")
        elapsed = time.monotonic() - t0
        mutate = await _cmd(c, wd, "/plan")
        await app.state.manager.wait("t1")
    assert read.status_code == 200 and elapsed < 0.8
    assert mutate.status_code == 409


async def test_commands_list_includes_user_commands(api, tmp_path):
    (tmp_path / ".luna" / "commands").mkdir(parents=True)
    (tmp_path / ".luna" / "commands" / "ship.md").write_text(
        "---\ndescription: ship it\n---\nship $ARGUMENTS\n"
    )
    c, _, wd = api()
    async with c:
        cmds = (await c.get("/commands", params={"workdir": wd})).json()["commands"]
    assert {"name": "/ship", "help": "ship it", "kind": "prompt"} in cmds
    assert any(x["name"] == "/model" and x["kind"] == "mutate" for x in cmds)


async def test_mutate_command_holds_the_session_lock(api, monkeypatch):
    """A long /verify must block a turn on the same session (no parallel writes)."""
    import time

    from luna.commands import builtin

    monkeypatch.setattr(
        builtin, "run_verify", lambda cmd, wd, **_: (time.sleep(0.8), (True, ""))[1]
    )
    c, app, wd = api()
    runtime = await app.state.runtimes.get("t1", wd)
    monkeypatch.setattr(type(runtime), "config", lambda self: _cfg_with_verify(self.workdir))
    async with c:
        cmd = asyncio.create_task(_cmd(c, wd, "/verify"))
        await asyncio.sleep(0.3)
        resp, _ = await turn(c, "t1", wd, "hi")
        await cmd
    assert resp.status_code == 409


def _cfg_with_verify(workdir):
    from luna.config.config import LunaConfig

    return LunaConfig(workdir=workdir, verify_command="make check")


async def test_read_command_does_not_save_state(api, monkeypatch):
    c, app, wd = api()
    runtime = await app.state.runtimes.get("t1", wd)
    saves = []
    monkeypatch.setattr(runtime, "save", lambda: saves.append(1))
    async with c:
        await _cmd(c, wd, "/context")
    assert saves == []


async def test_init_command_does_not_arm_a_reload_by_itself(api, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "init_prompt", lambda wd: "WRITE AGENTS.md")
    c, app, wd = api()
    async with c:
        result = (await _cmd(c, wd, "/init")).json()
        runtime = await app.state.runtimes.get("t1", wd)
    assert result["prompt"] == "WRITE AGENTS.md"
    assert result["effects"] == {"reload_after_turn": True}
    assert runtime.reload_after_turn is False


async def test_reload_and_model_switch_rebuild_through_the_runtime(api):
    built = []
    c, app, wd = api(built=built)
    async with c:
        reload = (await _cmd(c, wd, "/reload")).json()
        switched = (await _cmd(c, wd, "/model other-model")).json()
        state = (await c.get("/sessions/t1/state", params={"workdir": wd})).json()
    assert reload["effects"] == {"agent_rebuilt": True}
    assert switched["notices"][0]["level"] == "info"
    assert state["model"] == "other-model" and built == ["t1", "t1"]


async def test_diff_and_compact_act_on_the_core_session(api):
    c, _, wd = api(default=[FakeProvider.text("ok")])
    async with c:
        diff = (await _cmd(c, wd, "/diff")).json()
        await turn(c, "t1", wd, "hi")
        compact = (await _cmd(c, wd, "/compact")).json()
    assert diff["notices"] == [{"level": "dim", "text": "no changes this session"}]
    assert compact["notices"][0]["level"] in ("dim", "info")


async def test_new_core_commands_over_http(api):
    c, _, wd = api(default=[FakeProvider.text("ok"), FakeProvider.text("side answer")])
    async with c:
        await turn(c, "t1", wd, "hello")
        tools = (await _cmd(c, wd, "/tools")).json()
        btw = (await _cmd(c, wd, "/btw and?")).json()
        asked = (await _cmd(c, wd, "/rewind")).json()
        done = (await _cmd(c, wd, "/rewind 1 --yes")).json()
        history = (await c.get("/sessions/t1/messages", params={"workdir": wd})).json()
        fork = (await _cmd(c, wd, "/fork")).json()
    assert "write_todos" in tools["text"]
    assert btw["text"] == "side answer"
    assert asked["confirm"]["resubmit"] == "/rewind 1 --yes"
    assert done["effects"] == {"undo_depth": 0} and history["messages"] == []
    assert fork["effects"]["session"] != "t1"


async def test_commands_that_need_no_agent_work_with_a_broken_config(api, tmp_path):
    (tmp_path / ".luna.toml").write_text("[agent\n")  # unparsable
    c, _, wd = api()
    async with c:
        plan = await _cmd(c, wd, "/plan")
    assert plan.status_code == 200


async def test_undo_redo_rewind_are_409_while_background_tasks_run(api):
    c, app, wd = api()
    async with c:
        runtime = await app.state.runtimes.get("t1", wd)
        gate = asyncio.Event()
        handle = runtime.background.start("slow", gate.wait, session=runtime.session)
        await asyncio.sleep(0)
        try:
            codes = [
                (await _cmd(c, wd, line)).status_code
                for line in ("/undo --yes", "/redo", "/rewind 1 --yes")
            ]
            plan = await _cmd(c, wd, "/plan")
        finally:
            gate.set()
            await runtime.background.wait(handle)
    assert codes == [409, 409, 409]
    assert plan.status_code == 200
