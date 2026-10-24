import asyncio
import json
import sys
from pathlib import Path

import pytest
from luna_core.acp import AcpBridge, LunaAcpAgent, prompt_to_input
from luna_core.events import (
    ApprovalRequest,
    Cancelled,
    Notice,
    TextDelta,
    ThinkingDelta,
    TodosUpdated,
    ToolFinished,
    ToolStarted,
    TurnDone,
    UsageUpdate,
)
from luna_core.messages import Image
from luna_core.policy import Decision
from luna_core.session import EventLog, Session
from luna_core.usage import Usage

FIXTURE = str(Path(__file__).with_name("acp_fake_agent.py"))


class _FakeClient:
    """A minimal ACP client double: records updates, answers permission requests."""

    def __init__(self, outcome: dict | None = None) -> None:
        self.updates: list[dict] = []
        self.outcome = outcome
        self.permission_calls = 0

    async def session_update(self, *, session_id, update):
        self.updates.append(update)

    async def request_permission(self, *, session_id, tool_call, options):
        self.permission_calls += 1
        if self.outcome is None:
            raise AssertionError("request_permission should not be called in this test")
        return self.outcome


def test_bridge_updates():
    b = AcpBridge()
    assert b.updates(TextDelta("hi")) == [
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    ]
    assert b.updates(ThinkingDelta("hm"))[0]["sessionUpdate"] == "agent_thought_chunk"
    start = b.updates(ToolStarted("c1", "edit_file", {"file_path": "a.py"}, "a.py"))[0]
    assert start == {
        "sessionUpdate": "tool_call",
        "toolCallId": "c1",
        "title": "edit_file a.py",
        "kind": "edit",
        "status": "in_progress",
        "rawInput": {"file_path": "a.py"},
    }
    done = b.updates(ToolFinished("c1", "edit_file", False, "old_string not found", 0.1))[0]
    assert (
        done["status"] == "failed"
        and done["content"][0]["content"]["text"] == "old_string not found"
    )
    plan = b.updates(TodosUpdated([{"content": "x", "status": "in_progress"}]))[0]
    assert plan == {
        "sessionUpdate": "plan",
        "entries": [{"content": "x", "priority": "medium", "status": "in_progress"}],
    }
    assert b.updates(Notice("warn", "careful"))[0]["content"]["text"] == "\n_careful_\n"
    assert b.updates(Notice("dim", "quiet")) == []
    assert b.updates(UsageUpdate(Usage(input=10, output=5))) == []
    assert b.updates(UsageUpdate(Usage(input=10, output=5)), window=1000) == [
        {"sessionUpdate": "usage_update", "used": 15, "size": 1000}
    ]


def test_permission_round_trip():
    b = AcpBridge()
    req = ApprovalRequest("c2", "execute", {"command": "pytest"}, "pytest", "execute:pytest *")
    tool_call, options = b.permission(req)
    assert tool_call["toolCallId"] == "c2" and [o["optionId"] for o in options] == [
        "allow_once",
        "allow_always",
        "reject_once",
    ]
    assert b.decision({"outcome": "selected", "optionId": "allow_once"}, req) == Decision.approve()
    assert b.decision({"outcome": "selected", "optionId": "allow_always"}, req) == Decision.approve(
        always="execute:pytest *"
    )
    assert b.decision({"outcome": "selected", "optionId": "reject_once"}, req).type == "reject"
    assert b.decision({"outcome": "cancelled"}, req).type == "reject"


def test_stop_reasons_and_prompt_input():
    b = AcpBridge()
    assert b.stop_reason([TurnDone(Usage(), None, "end")]) == "end_turn"
    assert b.stop_reason([TurnDone(Usage(), None, "max_tokens")]) == "max_tokens"
    assert b.stop_reason([Cancelled()]) == "cancelled"
    text, images = prompt_to_input(
        [
            {"type": "text", "text": "fix"},
            {"type": "image", "mimeType": "image/png", "data": "AAA"},
            {"type": "resource", "resource": {"uri": "file:///a.py", "text": "x = 1"}},
            {"type": "resource_link", "uri": "file:///b.py", "name": "b.py"},
        ]
    )
    assert (
        text.startswith("fix")
        and "<attached: file:///a.py>\nx = 1" in text
        and "file:///b.py" in text
    )
    assert images == [Image("image/png", "AAA")]


def _make_fake_luna_agent(ws_root):
    from luna_core.loop import Agent
    from luna_core.policy import Policy
    from luna_core.providers.fake import FakeProvider
    from luna_core.workspace import Workspace

    script = [
        FakeProvider.call("execute", {"command": "echo hi"}, "c1"),
        FakeProvider.text("done: hi"),
    ]
    ws = Workspace(ws_root)
    return Agent(FakeProvider(script), "fake", workspace=ws, policy=Policy(workspace=ws))


async def test_ask_failure_rejects_instead_of_hanging(tmp_path, monkeypatch):
    """A broken ``bridge.permission`` must reject the call, not hang the turn forever."""
    pytest.importorskip("acp")
    log = EventLog(tmp_path / "acp.db")
    agent = LunaAcpAgent(lambda cwd: _make_fake_luna_agent(cwd), log)
    client = _FakeClient()
    agent.on_connect(client)
    new = await agent.new_session(cwd=str(tmp_path))
    session_id = new.session_id

    def _boom(request):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent.bridge, "permission", _boom)

    response = await asyncio.wait_for(
        agent.prompt(prompt=[{"type": "text", "text": "run it"}], session_id=session_id),
        timeout=5,
    )
    assert response.stop_reason == "end_turn"
    assert client.permission_calls == 0
    _, session = agent._sessions[session_id]
    assert session.pending_approvals == []


async def test_load_session_uses_session_workdir(tmp_path):
    """The agent is built for the session's own workdir, not the client-supplied ``cwd``."""
    pytest.importorskip("acp")
    log = EventLog(tmp_path / "acp.db")
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    session = Session.create(log, str(real_dir))

    seen_cwd = []

    def make_agent(cwd):
        seen_cwd.append(cwd)
        return _make_fake_luna_agent(real_dir)

    agent = LunaAcpAgent(make_agent, log)
    agent.on_connect(_FakeClient())
    await agent.load_session(cwd="/some/other/editor/cwd", session_id=session.id)
    assert seen_cwd == [str(real_dir)]


async def test_acp_stdio_end_to_end(tmp_path):
    pytest.importorskip("acp")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        FIXTURE,
        str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    async def send(obj):
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        await proc.stdin.drain()

    async def read():
        return json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))

    await send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {}},
        }
    )
    assert (await read())["result"]["protocolVersion"] == 1
    await send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {"cwd": str(tmp_path), "mcpServers": []},
        }
    )
    session_id = (await read())["result"]["sessionId"]
    await send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "run it"}]},
        }
    )
    updates, final = [], None
    while final is None:
        msg = await read()
        if msg.get("method") == "session/request_permission":
            await send(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
                }
            )
        elif msg.get("method") == "session/update":
            updates.append(msg["params"]["update"])
        elif msg.get("id") == 3:
            final = msg
    kinds = [u["sessionUpdate"] for u in updates]
    assert "tool_call" in kinds and "tool_call_update" in kinds and "agent_message_chunk" in kinds
    assert final["result"]["stopReason"] == "end_turn"
    proc.stdin.close()
    proc.kill()
    await proc.wait()


async def test_acp_child_approval_reaches_the_subagent(tmp_path):
    """A subagent's prefixed approval id is answered through the parent session."""
    pytest.importorskip("acp")
    log = EventLog(tmp_path / "acp.db")
    agent = LunaAcpAgent(lambda cwd: _make_fake_luna_agent(cwd), log)
    agent.on_connect(_FakeClient({"outcome": {"outcome": "selected", "optionId": "allow_once"}}))
    parent = Session.create(log, str(tmp_path))
    child = Session.create(log, str(tmp_path), undo=False)
    parent.adopt(child)
    fut = child.expect_decision("c1")
    request = ApprovalRequest(f"{child.id}:c1", "execute", {"command": "ls"}, "ls", "")
    await asyncio.wait_for(agent._ask(parent.id, parent, request), timeout=5)
    assert fut.done() and fut.result().type == "approve"
    assert parent.pending_approvals == []


def test_permission_always_label_names_every_rule():
    b = AcpBridge()
    req = ApprovalRequest(
        "c3",
        "execute",
        {"command": "cd x && ls"},
        "",
        "execute:cd *",
        ("execute:cd *", "execute:ls *"),
    )
    _, options = b.permission(req)
    assert options[1]["name"] == "Always allow (execute:cd *, execute:ls *)"


async def test_prompt_for_an_unknown_session_is_an_acp_error(tmp_path):
    acp = pytest.importorskip("acp")
    log = EventLog(tmp_path / "acp.db")
    agent = LunaAcpAgent(lambda cwd: _make_fake_luna_agent(cwd), log)
    agent.on_connect(_FakeClient())
    with pytest.raises(acp.RequestError) as info:
        await agent.prompt(prompt=[{"type": "text", "text": "hi"}], session_id="nope")
    assert info.value.code == -32602 and info.value.data["sessionId"] == "nope"


async def test_load_session_for_an_unknown_session_is_an_acp_error(tmp_path):
    acp = pytest.importorskip("acp")
    log = EventLog(tmp_path / "acp.db")
    agent = LunaAcpAgent(lambda cwd: _make_fake_luna_agent(cwd), log)
    agent.on_connect(_FakeClient())
    with pytest.raises(acp.RequestError) as info:
        await agent.load_session(cwd=str(tmp_path), session_id="nope")
    assert info.value.code == -32602 and info.value.data["sessionId"] == "nope"
