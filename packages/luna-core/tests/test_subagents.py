import asyncio
import subprocess
import time

import pytest
from luna_core.events import NeedsApproval, Notice, TurnDone
from luna_core.loop import Agent
from luna_core.messages import Message, ToolCall
from luna_core.policy import Decision, Policy
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session
from luna_core.subagents import (
    BUILTIN_SUBAGENTS,
    SubagentConfigError,
    SubagentRunner,
    SubagentSpec,
    load_subagents,
    task_tool,
)
from luna_core.tools import builtin_tools
from luna_core.tools.base import tool
from luna_core.workspace import Workspace


class Routing:
    """Child provider that picks a script by a marker in the delegated task text."""

    name = "fake"

    def __init__(self, routes):
        self.routes = {k: FakeProvider(v) for k, v in routes.items()}

    async def stream(self, request):
        task = request.messages[0].text
        key = next(k for k in self.routes if k in task)
        async for ev in self.routes[key].stream(request):
            yield ev


def git(root, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def log(tmp_path):
    lg = EventLog(tmp_path / "db" / "s.db")
    yield lg
    lg.close()


def parent_agent(
    root, script, child_provider, *, tools=None, specs=None, yolo=True, max_parallel=4
):
    runner = SubagentRunner(
        specs or dict(BUILTIN_SUBAGENTS),
        max_parallel=max_parallel,
        model_for=lambda spec: (child_provider, "child-model"),
    )
    ws = Workspace(root)
    agent = Agent(
        FakeProvider(script),
        "parent-model",
        workspace=ws,
        tools=[*(tools or builtin_tools()), task_tool(runner)],
        policy=Policy(workspace=ws, yolo=yolo),
    )
    return agent


def test_load_subagents_validation(tmp_path):
    good = tmp_path / "a.toml"
    good.write_text(
        '[subagent.researcher]\ndescription = "mine"\nprompt = "p"\n'
        '[subagent.fixer]\ntools = ["read_file", "edit_file"]\nunsafe = true\nisolation = "none"\n'
    )
    specs = load_subagents([good, tmp_path / "missing.toml"])
    assert (
        specs["researcher"].description == "mine"
        and specs["fixer"].writes
        and specs["fixer"].isolation == "none"
    )
    assert "general-purpose" in specs
    bad = tmp_path / "b.toml"
    bad.write_text('[subagent.x]\ntools = ["edit_file"]\n')
    with pytest.raises(SubagentConfigError, match="unsafe = true"):
        load_subagents([bad])
    bad.write_text('[subagent.x]\ntools = ["teleport"]\n')
    with pytest.raises(SubagentConfigError, match="unknown tools"):
        load_subagents([bad])


async def test_delegation_returns_the_child_answer(tmp_path, log):
    child = Routing({"bugs": [FakeProvider.text("found 3 bugs")]})
    agent = parent_agent(
        tmp_path,
        [
            FakeProvider.call(
                "task", {"description": "look for bugs", "subagent_type": "researcher"}, "t1"
            ),
            FakeProvider.text("ok"),
        ],
        child,
    )
    session = Session.create(log, str(tmp_path), undo=False)
    events = [e async for e in agent.run(session, "go")]
    assert isinstance(events[-1], TurnDone)
    assert "found 3 bugs" in agent.provider.requests[1].messages[-1].results[0].text
    kids = log.children(session.id)
    assert (
        len(kids) == 1
        and kids[0].meta["kind"] == "subagent"
        and kids[0].meta["agent"] == "researcher"
    )


async def test_parallel_children_run_concurrently(tmp_path, log):
    both = asyncio.Event()
    count = {"n": 0}

    @tool()
    async def rendezvous() -> str:
        """Wait for the sibling."""
        count["n"] += 1
        if count["n"] == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 3)
        return "met"

    specs = {"r": SubagentSpec("r", "rendezvous", "p", tools=("rendezvous",))}
    child = Routing(
        {
            "A": [FakeProvider.call("rendezvous", {}), FakeProvider.text("A done")],
            "B": [FakeProvider.call("rendezvous", {}), FakeProvider.text("B done")],
        }
    )
    two = Message.assistant(
        [
            ToolCall("t1", "task", {"description": "A", "subagent_type": "r"}),
            ToolCall("t2", "task", {"description": "B", "subagent_type": "r"}),
        ]
    )
    agent = parent_agent(
        tmp_path, [two, FakeProvider.text("ok")], child, tools=[rendezvous], specs=specs
    )
    session = Session.create(log, str(tmp_path), undo=False)
    [e async for e in agent.run(session, "go")]
    texts = [r.text for r in session.messages()[2].results]
    assert "A done" in texts[0] and "B done" in texts[1]


async def test_worktrees_keep_parallel_writers_apart(tmp_path, log):
    git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("hi\n")
    (tmp_path / "wip.txt").write_text("uncommitted parent edit\n")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-qm", "init")
    child = Routing(
        {
            "first": [
                FakeProvider.call("write_file", {"file_path": "notes.md", "content": "one\n"}),
                FakeProvider.text("wrote one"),
            ],
            "second": [
                FakeProvider.call("write_file", {"file_path": "notes.md", "content": "two\n"}),
                FakeProvider.text("wrote two"),
            ],
        }
    )
    two = Message.assistant(
        [
            ToolCall("t1", "task", {"description": "first"}),
            ToolCall("t2", "task", {"description": "second"}),
        ]
    )
    agent = parent_agent(tmp_path, [two, FakeProvider.text("ok")], child)
    session = Session.create(log, str(tmp_path), undo=False)
    [e async for e in agent.run(session, "go")]
    r1, r2 = (r.text for r in session.messages()[2].results)
    assert "luna/sub/" in r1 and "notes.md" in r1 and "git apply" in r1 and "luna/sub/" in r2
    assert not (tmp_path / "notes.md").exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "luna/sub/*"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.split()
    contents = sorted(
        subprocess.run(
            ["git", "show", f"{b}:notes.md"], cwd=tmp_path, capture_output=True, text=True
        ).stdout
        for b in branches
    )
    assert contents == ["one\n", "two\n"]
    seen_wip = subprocess.run(
        ["git", "show", f"{branches[0]}:wip.txt"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert (
        seen_wip == "uncommitted parent edit\n"
    )  # the child started from the parent's working tree
    assert not any((tmp_path / ".luna" / "worktrees").iterdir())


async def test_shared_workspace_serialises_writes_to_one_path(tmp_path, log):
    active = {"now": 0, "max": 0}

    @tool(mutating=True)
    async def slow_write(file_path: str) -> str:
        """Pretend to write slowly."""
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.1)
        active["now"] -= 1
        return "ok"

    specs = {
        "w": SubagentSpec("w", "writer", "p", tools=("slow_write",), unsafe=True, isolation="none")
    }
    child = Routing(
        {
            "A": [FakeProvider.call("slow_write", {"file_path": "x"}), FakeProvider.text("A")],
            "B": [FakeProvider.call("slow_write", {"file_path": "x"}), FakeProvider.text("B")],
        }
    )
    two = Message.assistant(
        [
            ToolCall("t1", "task", {"description": "A", "subagent_type": "w"}),
            ToolCall("t2", "task", {"description": "B", "subagent_type": "w"}),
        ]
    )
    agent = parent_agent(
        tmp_path, [two, FakeProvider.text("ok")], child, tools=[slow_write], specs=specs
    )
    [e async for e in agent.run(Session.create(log, str(tmp_path), undo=False), "go")]
    assert active["max"] == 1


async def test_child_approval_is_answered_through_the_parent(tmp_path, log):
    child = Routing(
        {
            "run": [
                FakeProvider.call("execute", {"command": "echo child"}, "k1"),
                FakeProvider.text("ran it"),
            ]
        }
    )
    specs = {
        "x": SubagentSpec("x", "runner", "p", tools=("execute",), unsafe=True, isolation="none")
    }
    agent = parent_agent(
        tmp_path,
        [
            FakeProvider.call("task", {"description": "run", "subagent_type": "x"}, "t1"),
            FakeProvider.text("ok"),
        ],
        child,
        specs=specs,
        yolo=False,
    )
    session = Session.create(log, str(tmp_path), undo=False)
    asked = []
    async for ev in agent.run(session, "go"):
        if isinstance(ev, NeedsApproval):
            asked.append(ev.request.call_id)
            session.decide(ev.request.call_id, Decision.approve())
    assert asked == ["k1"]
    assert "ran it" in session.messages()[2].results[0].text


async def test_parent_cancel_stops_the_child(tmp_path, log):
    child = Routing({"sleep": [FakeProvider.call("execute", {"command": "sleep 30"}, "k1")]})
    specs = {
        "x": SubagentSpec("x", "runner", "p", tools=("execute",), unsafe=True, isolation="none")
    }
    agent = parent_agent(
        tmp_path,
        [FakeProvider.call("task", {"description": "sleep", "subagent_type": "x"}, "t1")],
        child,
        specs=specs,
    )
    session = Session.create(log, str(tmp_path), undo=False)
    t0 = time.monotonic()
    async for ev in agent.run(session, "go"):
        if isinstance(ev, Notice) and "execute" in ev.text:
            session.cancel_turn()
    assert time.monotonic() - t0 < 6


async def test_budget_is_checked_before_spawning(tmp_path, log):
    # Siblings started in one message can push the session over budget; the runner re-checks.
    runner = SubagentRunner(dict(BUILTIN_SUBAGENTS), model_for=lambda spec: (Routing({}), "c"))
    ws = Workspace(tmp_path)
    parent = Agent(FakeProvider([]), "m", workspace=ws, max_cost=0.0)
    result = await runner.run(
        parent, Session.create(log, str(tmp_path), undo=False), "researcher", "x"
    )
    assert not result.ok and "budget reached" in result.error


async def test_worktree_isolation_without_git_is_an_error(tmp_path, log):
    agent = parent_agent(
        tmp_path,
        [
            FakeProvider.call("task", {"description": "x", "isolation": "worktree"}, "t1"),
            FakeProvider.text("ok"),
        ],
        Routing({}),
    )
    session = Session.create(log, str(tmp_path), undo=False)
    [e async for e in agent.run(session, "go")]
    result = session.messages()[2].results[0]
    assert result.is_error and "git repository" in result.text
