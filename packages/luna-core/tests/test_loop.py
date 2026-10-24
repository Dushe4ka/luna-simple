import asyncio
import time

import pytest
from luna_core.compaction import Compactor
from luna_core.errors import LunaCoreError, ProviderError
from luna_core.events import (
    Cancelled,
    NeedsApproval,
    Notice,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnDone,
    TurnError,
    TurnStarted,
    UsageUpdate,
)
from luna_core.hooks import HookResult, Hooks
from luna_core.loop import Agent, PathLocks
from luna_core.messages import Message, ToolCall, message_to_dict
from luna_core.policy import Decision, Policy, Rules
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session
from luna_core.tools.base import tool
from luna_core.workspace import Workspace


@pytest.fixture
def env(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    log = EventLog(tmp_path / ".luna" / "sessions.db")
    session = Session.create(log, str(tmp_path), undo=False)
    yield tmp_path, session
    log.close()


def agent_for(tmp_path, script, **kw):
    ws = Workspace(tmp_path)
    kw.setdefault("policy", Policy(workspace=ws))
    return Agent(FakeProvider(script), "fake-model", workspace=ws, **kw)


async def collect(agent, session, text="hi", decide=None):
    events = []
    async for ev in agent.run(session, text):
        events.append(ev)
        if isinstance(ev, NeedsApproval) and decide:
            session.decide(ev.request.call_id, decide(ev.request))
    return events


async def test_plain_answer(env):
    tmp, session = env
    events = await collect(agent_for(tmp, [FakeProvider.text("hello")]), session)
    kinds = [type(e) for e in events]
    assert (
        kinds[0] is TurnStarted
        and TextDelta in kinds
        and UsageUpdate in kinds
        and kinds[-1] is TurnDone
    )
    assert [m.text for m in session.messages()] == ["hi", "hello"]


async def test_tool_round_trip_without_approval_for_reads(env):
    tmp, session = env
    fake_script = [
        FakeProvider.call("read_file", {"file_path": "a.py"}, "c1"),
        FakeProvider.text("x is 1"),
    ]
    agent = agent_for(tmp, fake_script)
    events = await collect(agent, session)
    started = [e for e in events if isinstance(e, ToolStarted)]
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert started[0].name == "read_file" and finished[0].ok
    second_request = agent.provider.requests[1]
    assert "x = 1" in second_request.messages[-1].results[0].text


async def test_approval_approve_edit_reject_and_always(env):
    tmp, session = env
    script = [
        Message.assistant(
            [
                ToolCall("c1", "execute", {"command": "echo one"}),
                ToolCall("c2", "execute", {"command": "echo two"}),
                ToolCall("c3", "execute", {"command": "echo three"}),
            ]
        ),
        FakeProvider.text("done"),
    ]
    agent = agent_for(tmp, script)
    answers = {
        "c1": Decision.approve(always="execute:echo one"),
        "c2": Decision.edit({"command": "echo edited"}),
        "c3": Decision.reject("not now"),
    }
    events = await collect(agent, session, decide=lambda req: answers[req.call_id])
    asked = [e.request.call_id for e in events if isinstance(e, NeedsApproval)]
    assert asked == ["c1", "c2", "c3"]
    results = session.messages()[-2].results
    assert "one" in results[0].text and "edited" in results[1].text
    assert results[2].is_error and "rejected by the user: not now" in results[2].text
    assert "execute:echo one" in agent.policy.rules.allow


async def test_deny_rule_is_an_error_result(env):
    tmp, session = env
    ws = Workspace(tmp)
    agent = agent_for(
        tmp,
        [FakeProvider.call("execute", {"command": "rm -rf ."}), FakeProvider.text("ok")],
        policy=Policy(Rules(deny=["execute:rm *"]), workspace=ws),
    )
    events = await collect(agent, session)
    assert not any(isinstance(e, NeedsApproval) for e in events)
    assert "permission rule" in session.messages()[2].results[0].text


async def test_unknown_tool_and_invalid_json(env):
    tmp, session = env
    bad = Message.assistant(
        [ToolCall("c1", "nope", {}), ToolCall("c2", "read_file", {"__invalid_json__": "{"})]
    )
    await collect(agent_for(tmp, [bad, FakeProvider.text("ok")]), session)
    r1, r2 = session.messages()[2].results
    assert r1.is_error and "unknown tool" in r1.text and r2.is_error and "not valid JSON" in r2.text


async def test_reads_run_in_parallel(env):
    tmp, session = env
    started = asyncio.Event()
    count = {"n": 0}

    @tool()
    async def probe(n: int) -> str:
        """Wait until both probes run."""
        count["n"] += 1
        if count["n"] == 2:
            started.set()
        await asyncio.wait_for(started.wait(), 2)
        return f"probe {n}"

    script = [
        Message.assistant([ToolCall("a", "probe", {"n": 1}), ToolCall("b", "probe", {"n": 2})]),
        FakeProvider.text("ok"),
    ]
    await collect(agent_for(tmp, script, tools=[probe]), session)
    assert [r.text for r in session.messages()[2].results] == ["probe 1", "probe 2"]


async def test_mutations_on_one_path_are_serialised(env):
    tmp, session = env
    active = {"now": 0, "max": 0}

    @tool(mutating=True)
    async def touch(file_path: str) -> str:
        """Pretend to write."""
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.05)
        active["now"] -= 1
        return "ok"

    script = [
        Message.assistant(
            [
                ToolCall("a", "touch", {"file_path": "x"}),
                ToolCall("b", "touch", {"file_path": "/x"}),
            ]
        ),
        FakeProvider.text("ok"),
    ]
    await collect(
        agent_for(tmp, script, tools=[touch], policy=Policy(workspace=Workspace(tmp), yolo=True)),
        session,
    )
    assert active["max"] == 1


async def test_provider_error_keeps_partial_text(env):
    tmp, session = env
    script = [[TextDelta("partial "), ProviderError("connection reset", "network")]]
    events = await collect(agent_for(tmp, script), session)
    assert isinstance(events[-1], TurnError) and "connection reset" in events[-1].message
    assert session.messages()[-1].text == "partial "


async def test_auth_error_names_the_fix(env):
    tmp, session = env
    events = await collect(
        agent_for(tmp, [[ProviderError("HTTP 401: bad key", "auth", 401)]]), session
    )
    assert "API key" in events[-1].message and events[-1].kind == "auth"


async def test_cancel_kills_running_tool_and_patches_history(env):
    tmp, session = env
    marker = tmp / "late.txt"
    script = [
        FakeProvider.call("execute", {"command": f"sleep 3 && touch {marker}"}, "c1"),
        FakeProvider.text("after"),
    ]
    agent = agent_for(tmp, script, policy=Policy(workspace=Workspace(tmp), yolo=True))
    events = []
    t0 = time.monotonic()
    async for ev in agent.run(session, "go"):
        events.append(ev)
        if isinstance(ev, ToolStarted):
            session.cancel_turn()
    assert isinstance(events[-1], Cancelled) and time.monotonic() - t0 < 3
    await asyncio.sleep(3.2)
    assert not marker.exists()
    last = session.messages()[-1]
    assert last.role == "tool" and last.results[0].is_error and "cancelled" in last.results[0].text
    assert not session.busy
    events = await collect(agent, session, "again")
    assert isinstance(events[-1], TurnDone)


async def test_consumer_task_cancel_is_recorded(env):
    tmp, session = env
    agent = agent_for(
        tmp,
        [FakeProvider.call("execute", {"command": "sleep 5"}, "c1")],
        policy=Policy(workspace=Workspace(tmp), yolo=True),
    )

    async def consume():
        async for _ in agent.run(session, "go"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(r.type == "cancelled" for r in session.records()) and not session.busy


async def test_dangling_calls_from_a_crash_are_patched(env):
    tmp, session = env
    session.append("turn_started", {"turn_id": "old"})
    session.append("user", {"message": message_to_dict(Message.user("x"))})
    session.append(
        "assistant", {"message": message_to_dict(FakeProvider.call("ls", {}, "lost")), "usage": {}}
    )
    agent = agent_for(tmp, [FakeProvider.text("ok")])
    await collect(agent, session)
    sent = agent.provider.requests[0].messages
    assert sent[2].role == "tool" and sent[2].results[0].call_id == "lost"


async def test_max_steps(env):
    tmp, session = env
    script = [FakeProvider.call("ls", {}, f"c{i}") for i in range(3)]
    events = await collect(agent_for(tmp, script, max_steps=2), session)
    assert any(isinstance(e, Notice) and "max_steps" in e.text for e in events)
    assert isinstance(events[-1], TurnDone) and events[-1].stop_reason == "max_steps"


async def test_big_tool_output_is_saved_to_a_file(env):
    tmp, session = env
    (tmp / "big.txt").write_text("\n".join(f"line {i} " + "x" * 50 for i in range(3000)))
    script = [
        FakeProvider.call("read_file", {"file_path": "big.txt", "limit": 3000}, "c1"),
        FakeProvider.text("ok"),
    ]
    await collect(agent_for(tmp, script, tool_output_budget=5000), session)
    text = session.messages()[2].results[0].text
    assert len(text) < 6000 and ".luna/tool-output/c1.txt" in text
    assert (tmp / ".luna" / "tool-output" / "c1.txt").stat().st_size > 30_000


async def test_queued_message_arrives_at_the_next_step(env):
    tmp, session = env

    @tool()
    async def slow() -> str:
        """Takes a moment."""
        session.queue("also check tests")
        return "done"

    agent = agent_for(
        tmp, [FakeProvider.call("slow", {}, "c1"), FakeProvider.text("ok")], tools=[slow]
    )
    await collect(agent, session)
    assert agent.provider.requests[1].messages[-1].text == "also check tests"


async def test_turn_end_hook_can_send_the_agent_back_to_work(env):
    tmp, session = env
    hooks = Hooks()
    calls = {"n": 0}

    async def verify(payload):
        calls["n"] += 1
        return HookResult(block=True, message="tests fail: fix them") if calls["n"] == 1 else None

    hooks.add("turn_end", verify)
    agent = agent_for(tmp, [FakeProvider.text("done"), FakeProvider.text("fixed")], hooks=hooks)
    events = await collect(agent, session)
    assert isinstance(events[-1], TurnDone)
    assert [m.text for m in session.messages()] == ["hi", "done", "tests fail: fix them", "fixed"]


async def test_pre_tool_hook_blocks_and_session_start_adds_context(env):
    tmp, session = env
    hooks = Hooks()
    hooks.add("pre_tool", lambda p: _r(HookResult(block=True, message="nope")), match="read_file")
    hooks.add("session_start", lambda p: _r(HookResult(context="project uses uv")))
    agent = agent_for(
        tmp,
        [FakeProvider.call("read_file", {"file_path": "a.py"}), FakeProvider.text("ok")],
        hooks=hooks,
    )
    await collect(agent, session)
    msgs = session.messages()
    assert "project uses uv" in msgs[0].content[-1].text
    assert msgs[2].results[0].is_error and msgs[2].results[0].text == "nope"


async def _r(v):
    return v


async def test_busy_session_rejects_a_second_turn(env):
    tmp, session = env
    session._begin_turn()
    with pytest.raises(LunaCoreError):
        await collect(agent_for(tmp, [FakeProvider.text("x")]), session)
    session._end_turn()


def test_run_sync_without_handler_rejects_approvals(tmp_path):
    log = EventLog(tmp_path / "s.db")
    session = Session.create(log, str(tmp_path), undo=False)
    agent = agent_for(
        tmp_path, [FakeProvider.call("execute", {"command": "echo hi"}), FakeProvider.text("ok")]
    )
    events = agent.run_sync(session, "go")
    assert isinstance(events[-1], TurnDone)
    assert "no approval handler" in session.messages()[2].results[0].text


async def test_path_locks_are_reentrant_free_and_ordered():
    locks = PathLocks()
    async with locks.hold(["b", "a"]):
        assert locks.locked("a") and locks.locked("b")
    assert not locks.locked("a")


async def test_closing_the_generator_stops_tools_and_records(env):
    tmp, session = env
    marker = tmp / "late.txt"
    script = [FakeProvider.call("execute", {"command": f"sleep 2 && touch {marker}"}, "c1")]
    agent = agent_for(tmp, script, policy=Policy(workspace=Workspace(tmp), yolo=True))
    gen = agent.run(session, "go")
    async for ev in gen:
        if isinstance(ev, ToolStarted):
            break
    await gen.aclose()
    types = [r.type for r in session.records()]
    assert types[-2:] == ["tool_results", "cancelled"] and not session.busy
    assert session.messages()[-1].results[0].is_error
    await asyncio.sleep(2.3)
    assert not marker.exists()


async def test_cancel_during_approval_runs_nothing_from_the_batch(env):
    tmp, session = env
    calls = [
        ToolCall("c1", "write_file", {"file_path": "n.txt", "content": "x"}),
        ToolCall("c2", "execute", {"command": "echo two"}),
    ]
    agent = agent_for(tmp, [Message.assistant(calls)])
    events = []
    async for ev in agent.run(session, "go"):
        events.append(ev)
        if isinstance(ev, NeedsApproval):
            if ev.request.call_id == "c1":
                session.decide("c1", Decision.approve())
            else:
                session.cancel_turn()
    assert isinstance(events[-1], Cancelled)
    assert not (tmp / "n.txt").exists()
    results = session.messages()[-1].results
    assert [r.call_id for r in results] == ["c1", "c2"] and all(r.is_error for r in results)


async def test_tool_emitted_events_interleave(env):
    tmp, session = env

    @tool()
    async def chatty(ctx) -> str:
        """Emit a notice."""
        ctx.emit(Notice("info", "working"))
        return "ok"

    agent = agent_for(
        tmp, [FakeProvider.call("chatty", {}, "c1"), FakeProvider.text("ok")], tools=[chatty]
    )
    events = await collect(agent, session)
    kinds = [type(e) for e in events]
    i = kinds.index(ToolStarted)
    assert kinds[i : i + 3] == [ToolStarted, Notice, ToolFinished]


async def test_closing_after_turn_done_is_not_a_cancel(env):
    tmp, session = env
    gen = agent_for(tmp, [FakeProvider.text("hello")]).run(session, "hi")
    async for ev in gen:
        if isinstance(ev, TurnDone):
            break
    await gen.aclose()
    assert [r.type for r in session.records()][-1] == "turn_done" and not session.busy


class BadJournal:
    def begin_turn(self, turn_id):
        pass

    def before_write(self, turn_id, rel):
        raise RuntimeError("journal broke")


async def test_failure_around_a_tool_is_an_error_result_not_a_hang(env):
    tmp, session = env
    script = [
        FakeProvider.call("write_file", {"file_path": "n.txt", "content": "x"}, "c1"),
        FakeProvider.text("ok"),
    ]
    agent = agent_for(
        tmp,
        script,
        policy=Policy(workspace=Workspace(tmp), yolo=True),
        services={"undo_journal": BadJournal()},
    )
    events = await asyncio.wait_for(collect(agent, session), 5)
    assert isinstance(events[-1], TurnDone)
    result = session.messages()[2].results[0]
    assert result.is_error and "write_file failed: RuntimeError: journal broke" in result.text


async def test_unsaveable_overflow_is_still_clipped(env):
    tmp, session = env
    (tmp / ".luna" / "tool-output").write_text("not a dir")
    (tmp / "big.txt").write_text("\n".join(f"line {i} " + "x" * 50 for i in range(3000)))
    script = [
        FakeProvider.call("read_file", {"file_path": "big.txt", "limit": 3000}, "c1"),
        FakeProvider.text("ok"),
    ]
    events = await asyncio.wait_for(
        collect(agent_for(tmp, script, tool_output_budget=5000), session), 5
    )
    assert isinstance(events[-1], TurnDone)
    text = session.messages()[2].results[0].text
    assert len(text) < 6000 and "could not be saved" in text


async def test_invalid_path_argument_is_an_error_result(env):
    tmp, session = env
    script = [
        FakeProvider.call("write_file", {"file_path": "a\x00b", "content": "x"}, "c1"),
        FakeProvider.text("ok"),
    ]
    events = await asyncio.wait_for(collect(agent_for(tmp, script), session), 5)
    assert isinstance(events[-1], TurnDone)
    assert session.messages()[2].results[0].is_error


async def test_preparation_failure_is_an_error_result(env):
    tmp, session = env

    class BrokenPolicy(Policy):
        def check(self, tool, args):
            raise RuntimeError("policy broke")

    script = [FakeProvider.call("ls", {}, "c1"), FakeProvider.text("ok")]
    agent = agent_for(tmp, script, policy=BrokenPolicy(workspace=Workspace(tmp)))
    events = await asyncio.wait_for(collect(agent, session), 5)
    assert isinstance(events[-1], TurnDone)
    result = session.messages()[2].results[0]
    assert result.is_error and "ls failed: RuntimeError: policy broke" in result.text


class _BlockingProvider:
    """A provider whose ``stream`` never produces an event until told to."""

    name = "blocking"

    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        await asyncio.Event().wait()
        yield  # pragma: no cover - never reached; makes this an async generator


async def test_cancel_during_compaction_is_observed(env):
    tmp, session = env
    blocking = _BlockingProvider()
    compactor = Compactor(threshold=0.0, keep_turns=0, provider=blocking)
    agent = agent_for(tmp, [FakeProvider.text("hi")], compactor=compactor)

    async def consume():
        return [e async for e in agent.run(session, "go")]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    session.cancel_turn()
    events = await asyncio.wait_for(task, 2)
    assert isinstance(events[-1], Cancelled)
    assert blocking.requests
    assert not any(r.type == "compacted" for r in session.records())
    assert not session.busy


async def test_overflow_file_is_written_off_the_event_loop(env, monkeypatch):
    tmp, session = env
    offloaded = []
    real = asyncio.to_thread

    async def spy(fn, /, *args, **kwargs):
        offloaded.append(getattr(fn, "__name__", repr(fn)))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    (tmp / "big.txt").write_text("\n".join(f"line {i} " + "x" * 50 for i in range(3000)))
    script = [
        FakeProvider.call("read_file", {"file_path": "big.txt", "limit": 3000}, "c1"),
        FakeProvider.text("ok"),
    ]
    await collect(agent_for(tmp, script, tool_output_budget=5000), session)
    assert "_save_overflow" in offloaded


# -- final review C1: the loop records each turn's end so /undo can tell user
# -- edits made after the turn apart from the turn's own changes --


async def test_undo_after_a_turn_skips_a_file_the_user_edited_since(tmp_path):
    log = EventLog(tmp_path / ".luna" / "sessions.db")
    session = Session.create(log, str(tmp_path))
    script = [
        FakeProvider.call("write_file", {"file_path": "n.py", "content": "x = 2\n"}, "c1"),
        FakeProvider.text("done"),
    ]
    await collect(agent_for(tmp_path, script), session, decide=lambda r: Decision.approve())
    assert (tmp_path / "n.py").read_text() == "x = 2\n"
    (tmp_path / "n.py").write_text("x = 3  # user\n")
    res = session.undo()
    assert (tmp_path / "n.py").read_text() == "x = 3  # user\n"
    assert "skipped n.py: changed since the turn" in res.note
    log.close()


# -- re-review R1: git-mode undo restores file-tool paths only, unless a
# -- command ran — then the extra paths are restored and named --


def _git_project(tmp_path):
    import subprocess

    root = tmp_path / "proj"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "c.txt").write_text("user file\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True, capture_output=True)
    log = EventLog(tmp_path / "db" / "sessions.db")
    return root, log, Session.create(log, str(root))


async def test_git_undo_leaves_a_file_edited_outside_luna_during_the_turn(tmp_path):
    root, log, session = _git_project(tmp_path)

    @tool()
    async def editor() -> str:
        """Stands in for the user's editor saving a file while the turn runs."""
        (root / "c.txt").write_text("saved in the editor\n")
        return "ok"

    script = [
        Message.assistant(
            [
                ToolCall("c1", "write_file", {"file_path": "n.txt", "content": "new\n"}),
                ToolCall("c2", "editor", {}),
            ]
        ),
        FakeProvider.text("done"),
    ]
    agent = agent_for(root, script, policy=Policy(workspace=Workspace(root), yolo=True))
    agent.add_tool(editor)
    await collect(agent, session)
    res = session.undo()
    assert not (root / "n.txt").exists()
    assert (root / "c.txt").read_text() == "saved in the editor\n"
    assert "also restored" not in res.note
    log.close()


async def test_git_undo_after_a_command_restores_and_names_its_files(tmp_path):
    root, log, session = _git_project(tmp_path)
    script = [
        FakeProvider.call("execute", {"command": "printf changed > c.txt"}, "c1"),
        FakeProvider.text("done"),
    ]
    agent = agent_for(root, script, policy=Policy(workspace=Workspace(root), yolo=True))
    await collect(agent, session)
    assert (root / "c.txt").read_text() == "changed"
    res = session.undo()
    assert (root / "c.txt").read_text() == "user file\n"
    assert "also restored (changed by commands or outside Luna): c.txt" in res.note
    log.close()


async def test_turn_end_is_recorded_after_an_unexpected_error(tmp_path):
    log = EventLog(tmp_path / "db" / "s.db")
    session = Session.create(log, str(tmp_path))

    class Broken:
        name = "fake"

        async def stream(self, request):
            raise RuntimeError("provider bug")
            yield  # pragma: no cover

    agent = Agent(Broken(), "fake-model", workspace=Workspace(tmp_path))
    with pytest.raises(RuntimeError):
        await collect(agent, session)
    assert "end" in session.undo_journal._load()["turns"][-1]
    log.close()


async def test_turn_end_on_close_runs_off_the_event_loop_thread(tmp_path, monkeypatch):
    import threading

    from luna_core.undo import UndoJournal

    log = EventLog(tmp_path / "db" / "s.db")
    session = Session.create(log, str(tmp_path))
    threads = []
    real = UndoJournal.end_turn

    def spy(self, turn_id):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(self, turn_id)

    monkeypatch.setattr(UndoJournal, "end_turn", spy)
    agent = agent_for(tmp_path, [FakeProvider.text("hello")])
    run = agent.run(session, "hi")
    async for event in run:
        if isinstance(event, TextDelta):
            break
    await run.aclose()
    assert threads == [False]
    assert "end" in session.undo_journal._load()["turns"][-1]
    log.close()


async def test_always_allow_that_cannot_be_saved_still_runs_the_approved_call(env):
    tmp, session = env

    def broken_persist(rule):
        raise ValueError("permissions.toml would break")

    ws = Workspace(tmp)
    script = [
        FakeProvider.call("write_file", {"file_path": "n.txt", "content": "x"}, "c1"),
        FakeProvider.text("done"),
    ]
    agent = agent_for(tmp, script, policy=Policy(workspace=ws, persist_rule=broken_persist))
    events = await collect(
        agent, session, decide=lambda r: Decision.approve(always="write_file:n.txt")
    )
    assert (tmp / "n.txt").read_text() == "x"
    warns = [e for e in events if isinstance(e, Notice) and e.level == "warn"]
    assert any("permissions.toml would break" in w.text for w in warns)
    assert isinstance(events[-1], TurnDone)


async def test_always_on_a_compound_command_saves_every_rule(env):
    tmp, session = env
    (tmp / "src").mkdir()
    script = [
        FakeProvider.call("execute", {"command": "cd src && ls"}, "c1"),
        FakeProvider.call("execute", {"command": "cd src && ls"}, "c2"),
        FakeProvider.text("done"),
    ]
    agent = agent_for(tmp, script)
    asked = []

    def decide(request):
        asked.append(request.call_id)
        return Decision.approve(always=request.suggested_rule)  # what clients echo back

    await collect(agent, session, decide=decide)
    assert asked == ["c1"]  # the second identical call ran without a prompt
    assert {"execute:cd *", "execute:ls *"} <= set(agent.policy.rules.allow)


async def test_always_on_a_crafted_line_saves_no_catch_all_rule(env):
    from luna_core.events import Notice

    tmp, session = env
    script = [
        FakeProvider.call("execute", {"command": '"*" x'}, "c1"),
        FakeProvider.text("done"),
    ]
    agent = agent_for(tmp, script)
    events = await collect(
        agent, session, decide=lambda r: Decision.approve(always=r.suggested_rule)
    )
    assert agent.policy.rules.allow == []
    assert any(isinstance(e, Notice) and "not saved" in e.text for e in events)
