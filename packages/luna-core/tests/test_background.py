import asyncio

import pytest
from luna_core.background import AgentManager, BackgroundTasks
from luna_core.errors import LunaCoreError
from luna_core.events import BackgroundDone
from luna_core.loop import Agent
from luna_core.policy import Policy
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session
from luna_core.subagents import BUILTIN_SUBAGENTS, SubagentRunner, task_tool
from luna_core.tools import builtin_tools
from luna_core.tools.base import tool
from luna_core.tools.tasks import task_cancel_tool, task_status_tool
from luna_core.workspace import Workspace


@pytest.fixture
def log(tmp_path):
    lg = EventLog(tmp_path / "db" / "s.db")
    yield lg
    lg.close()


async def test_background_task_result_is_queued_logged_and_notified(tmp_path, log):
    session = Session.create(log, str(tmp_path), undo=False)
    seen = []
    session.subscribe(seen.append)
    tasks = BackgroundTasks()

    async def work():
        await asyncio.sleep(0.05)
        return "all good"

    handle = tasks.start("check", work, session=session)
    assert tasks.get(handle).status in ("queued", "running")
    info = await tasks.wait(handle)
    assert info.status == "done" and info.ok and info.summary == "all good"
    assert session.drain_queue() == [f"[background task {handle} · check · done]\nall good"]
    assert any(r.type == "background_done" for r in session.records())
    assert seen == [BackgroundDone(handle, True, "all good")]


async def test_background_cancel_and_failure(tmp_path, log):
    session = Session.create(log, str(tmp_path), undo=False)
    tasks = BackgroundTasks(max_parallel=1)

    async def forever():
        await asyncio.sleep(60)

    async def broken():
        raise RuntimeError("kaput")

    h1 = tasks.start("slow", forever, session=session)
    h2 = tasks.start("bad", broken, session=session)
    await asyncio.sleep(0.05)
    assert tasks.get(h2).status == "queued"  # max_parallel=1
    assert tasks.cancel(h1)
    assert (await tasks.wait(h1)).status == "cancelled"
    info = await tasks.wait(h2)
    assert info.status == "failed" and "kaput" in info.summary
    assert [t.handle for t in tasks.list(session.id)] == [h1, h2]


async def test_task_tool_background_and_status_tools(tmp_path, log):
    child = FakeProvider([FakeProvider.text("background finding")])
    runner = SubagentRunner(dict(BUILTIN_SUBAGENTS), model_for=lambda spec: (child, "c"))
    tasks = BackgroundTasks()
    ws = Workspace(tmp_path)
    script = [
        FakeProvider.call(
            "task", {"description": "dig", "subagent_type": "researcher", "background": True}, "t1"
        ),
        FakeProvider.call("task_status", {}, "s1"),
        FakeProvider.text("waiting"),
    ]
    agent = Agent(
        FakeProvider(script),
        "m",
        workspace=ws,
        policy=Policy(workspace=ws),
        tools=[*builtin_tools(), task_tool(runner), task_status_tool(), task_cancel_tool()],
        services={"background": tasks},
    )
    session = Session.create(log, str(tmp_path), undo=False)
    [e async for e in agent.run(session, "go")]
    results = {r.call_id: r for m in session.messages() if m.role == "tool" for r in m.results}
    handle = results["t1"].text.split("started background task ")[1].split(";")[0]
    assert handle in results["s1"].text
    await tasks.wait(handle)
    # Delivered either inside the running turn (next step boundary) or queued for the next one.
    delivered = session.drain_queue() + [m.text for m in session.messages() if m.role == "user"]
    assert any("background finding" in text for text in delivered)


async def test_background_session_survives_client_disconnect(tmp_path, log):
    gate = asyncio.Event()

    @tool()
    async def wait_gate() -> str:
        """Block until the test opens the gate."""
        await gate.wait()
        return "gate open"

    ws = Workspace(tmp_path)
    agent = Agent(
        FakeProvider([FakeProvider.call("wait_gate", {}), FakeProvider.text("finished the job")]),
        "m",
        workspace=ws,
        tools=[wait_gate],
    )
    session = Session.create(log, str(tmp_path), undo=False)
    done = []
    manager = AgentManager(on_done=done.append)
    info = manager.start(agent, session, "long job", label="job")
    assert info.status in ("queued", "running")
    with pytest.raises(LunaCoreError):
        manager.start(agent, session, "again")
    received = []
    unsubscribe = manager.subscribe(session.id, received.append)
    while not any(e["event"] == "tool_started" for e in received):
        await asyncio.sleep(0.01)
    unsubscribe()  # the TUI closes
    gate.set()
    final = await manager.wait(session.id)
    assert final.status == "done" and "finished the job" in final.text
    assert done == [final]
    assert not any(e["event"] == "turn_done" for e in received)  # nobody was listening any more
    replay = manager.events(session.id)
    assert replay[-1]["event"] == "turn_done" and [e["index"] for e in replay] == list(
        range(len(replay))
    )
    assert [e["event"] for e in manager.events(session.id, after=len(replay) - 2)] == ["turn_done"]
    reopened = Session.open(log, session.id, undo=False)
    assert reopened.messages()[-1].text == "finished the job"


async def test_manager_waiting_status_and_cancel(tmp_path, log):
    ws = Workspace(tmp_path)
    agent = Agent(
        FakeProvider([FakeProvider.call("execute", {"command": "echo hi"}, "c1")]),
        "m",
        workspace=ws,
    )
    session = Session.create(log, str(tmp_path), undo=False)
    manager = AgentManager()
    manager.start(agent, session, "go")
    while manager.get(session.id).status != "waiting":
        await asyncio.sleep(0.01)
    assert manager.cancel(session.id)
    assert (await manager.wait(session.id)).status == "cancelled"
    await manager.shutdown()


async def test_cancel_before_first_step_delivers_exactly_once(tmp_path, log):
    session = Session.create(log, str(tmp_path), undo=False)
    seen = []
    session.subscribe(seen.append)
    tasks = BackgroundTasks()

    async def work():
        await asyncio.sleep(60)

    handle = tasks.start("early", work, session=session)
    assert tasks.cancel(handle)  # before the task ever ran
    assert (await tasks.wait(handle)).status == "cancelled"
    await asyncio.sleep(0)
    assert session.drain_queue() == [f"[background task {handle} · early · cancelled]\ncancelled"]
    assert seen == [BackgroundDone(handle, False, "cancelled")]
    assert sum(r.type == "background_done" for r in session.records()) == 1

    ws = Workspace(tmp_path)
    agent = Agent(FakeProvider([FakeProvider.text("never")]), "m", workspace=ws)
    done = []
    manager = AgentManager(on_done=done.append)
    manager.start(agent, session, "go")
    await manager.shutdown()  # cancelled before the run's first step
    await asyncio.sleep(0)
    assert [i.status for i in done] == ["cancelled"]
    assert seen[1:] == [BackgroundDone(session.id, False, "")]


async def test_task_tools_do_not_reach_other_sessions_tasks(tmp_path, log):
    from luna_core.tools.base import ToolContext

    mine = Session.create(log, str(tmp_path), undo=False)
    other = Session.create(log, str(tmp_path), undo=False)
    tasks = BackgroundTasks()

    async def forever():
        await asyncio.sleep(60)

    handle = tasks.start("theirs", forever, session=other)
    ctx = ToolContext(session_id=mine.id, services={"background": tasks})
    status = await task_status_tool().invoke(ctx, {"handle": handle})
    assert status.content[0].text == f"no task {handle}"
    cancel = await task_cancel_tool().invoke(ctx, {"handle": handle})
    assert cancel.is_error and cancel.content[0].text == f"{handle} is not running"
    assert tasks.get(handle).status in ("queued", "running")
    own = ToolContext(session_id=other.id, services={"background": tasks})
    assert handle in (await task_status_tool().invoke(own, {"handle": handle})).content[0].text
    assert not (await task_cancel_tool().invoke(own, {"handle": handle})).is_error
    await tasks.shutdown()


async def test_background_tasks_retain_at_most_200_finished(tmp_path, log):
    session = Session.create(log, str(tmp_path), undo=False)
    tasks = BackgroundTasks()
    gate = asyncio.Event()

    async def slow():
        await gate.wait()

    async def quick():
        return "ok"

    live = tasks.start("live", slow, session=session)
    handles = [tasks.start(f"q{i}", quick, session=session) for i in range(205)]
    while set(tasks._tasks) != {live}:
        await asyncio.sleep(0.01)
    infos = tasks.list()
    assert len(infos) <= 200 + 1
    assert tasks.get(handles[0]) is None and tasks.get(handles[-1]) is not None
    with pytest.raises(LunaCoreError, match=f"no task {handles[0]}"):
        await tasks.wait(handles[0])  # evicted: a clear error, not a KeyError
    with pytest.raises(LunaCoreError, match="no task bg-unknown"):
        await tasks.wait("bg-unknown")
    assert tasks.get(live).status in ("queued", "running")
    assert set(tasks._tasks) == {live}  # finished tasks drop their asyncio.Task
    gate.set()
    assert (await tasks.wait(live)).status == "done"
    assert (await tasks.wait(handles[-1])).status == "done"  # finished handles still wait


async def test_agent_manager_retains_at_most_200_finished(tmp_path, log):
    ws = Workspace(tmp_path)
    gate = asyncio.Event()

    @tool()
    async def wait_gate() -> str:
        """Block until the test opens the gate."""
        await gate.wait()
        return "gate open"

    manager = AgentManager(max_parallel=8)
    live_session = Session.create(log, str(tmp_path), undo=False)
    slow = Agent(
        FakeProvider([FakeProvider.call("wait_gate", {}), FakeProvider.text("x")]),
        "m",
        workspace=ws,
        tools=[wait_gate],
    )
    manager.start(slow, live_session, "live")
    ids = []
    for i in range(205):
        s = Session.create(log, str(tmp_path), undo=False)
        manager.start(Agent(FakeProvider([FakeProvider.text(f"r{i}")]), "m", workspace=ws), s, "go")
        await manager.wait(s.id)
        ids.append(s.id)
    assert len(manager.list()) <= 200 + 1
    assert manager.get(ids[0]) is None and manager.events(ids[0]) == []
    with pytest.raises(LunaCoreError, match=f"no run for session {ids[0]}"):
        await manager.wait(ids[0])  # evicted: a clear error, not a KeyError
    with pytest.raises(LunaCoreError, match="no run for session nope"):
        await manager.wait("nope")
    assert manager.get(ids[-1]).status == "done"
    assert manager.get(live_session.id).status in ("running", "waiting")
    assert set(manager._tasks) == {live_session.id} and set(manager._sessions) == {live_session.id}
    gate.set()
    assert (await manager.wait(live_session.id)).status == "done"
    assert (await manager.wait(ids[-1])).status == "done"


async def test_agent_manager_event_index_is_monotonic_across_runs(tmp_path, log):
    ws = Workspace(tmp_path)
    session = Session.create(log, str(tmp_path), undo=False)
    agent = Agent(
        FakeProvider([FakeProvider.text("one"), FakeProvider.text("two")]), "m", workspace=ws
    )
    manager = AgentManager()
    manager.start(agent, session, "first")
    await manager.wait(session.id)
    last = manager.events(session.id)[-1]["index"]
    manager.start(agent, session, "second")
    await manager.wait(session.id)
    second = manager.events(session.id)
    assert second[0]["index"] > last
    assert [e["event"] for e in manager.events(session.id, after=last)][-1] == "turn_done"


def _boom(*_args):
    raise RuntimeError("listener exploded")


async def test_swallowed_callback_errors_are_logged(tmp_path, log, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="luna_core")
    session = Session.create(log, str(tmp_path), undo=False)
    session.subscribe(_boom)
    tasks = BackgroundTasks()

    async def work():
        return "ok"

    await tasks.wait(tasks.start("t", work, session=session))
    assert any("listener exploded" in r.getMessage() for r in caplog.records)
    caplog.clear()

    agent = Agent(FakeProvider([FakeProvider.text("hi")]), "m", workspace=Workspace(tmp_path))
    run_session = Session.create(log, str(tmp_path), undo=False)
    manager = AgentManager(on_done=_boom)
    manager.start(agent, run_session, "go")
    manager.subscribe(run_session.id, _boom)
    await manager.wait(run_session.id)
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("luna_core")]
    assert any("on_done" in m and "listener exploded" in m for m in messages)


def test_start_without_a_running_loop_leaves_no_phantom_entries(tmp_path, log):
    session = Session.create(log, str(tmp_path), undo=False)
    tasks = BackgroundTasks()  # built and used outside any running loop

    async def work():
        return "x"

    with pytest.raises(LunaCoreError, match="running event loop"):
        tasks.start("t", work, session=session)
    assert tasks.list() == [] and tasks._tasks == {}

    manager = AgentManager()
    agent = Agent(FakeProvider([]), "m", workspace=Workspace(tmp_path))
    with pytest.raises(LunaCoreError, match="running event loop"):
        manager.start(agent, session, "go")
    assert manager.list() == [] and manager._events == {}
