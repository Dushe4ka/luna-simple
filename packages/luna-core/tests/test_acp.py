import asyncio
import json
import sys
from pathlib import Path

import pytest
from luna_core.acp import AcpBridge, prompt_to_input
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
)
from luna_core.messages import Image
from luna_core.policy import Decision
from luna_core.usage import Usage

FIXTURE = str(Path(__file__).with_name("acp_fake_agent.py"))


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
