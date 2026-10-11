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
