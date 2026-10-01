import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from luna.server.app import create_app
from luna.server.run import make_session_agent_factory


def _parse(raw: str) -> list[dict]:
    return [
        json.loads(line[5:].strip())
        for block in raw.strip().split("\n\n")
        for line in block.splitlines()
        if line.startswith("data:")
    ]


def _write_call(path="a.txt", call_id="w1"):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": call_id,
                "name": "write_file",
                "args": {"file_path": f"/{path}", "content": "x"},
            }
        ],
    )


@pytest.fixture
def client_for(tmp_path):
    def make(*responses, verify="", yolo=False):
        (tmp_path / ".luna.toml").write_text(
            "[agent]\n"
            f'verify_command = "{verify}"\nformat_command = ""\ndiagnose_command = ""\n'
            f"yolo = {'true' if yolo else 'false'}\n"
        )
        from tests.conftest import FakeToolCallingModel

        model = FakeToolCallingModel(responses=list(responses))
        app = create_app(token="t", session_agent_factory=make_session_agent_factory(model=model))
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer t"},
        ), app

    return make


async def _say(c, tmp_path, text, thread="t1"):
    resp = await c.post(
        f"/sessions/{thread}/messages", json={"content": text, "workdir": str(tmp_path)}
    )
    return resp, _parse(resp.text)


async def test_two_sessions_in_one_workdir_are_isolated(client_for, tmp_path):
    c, app = client_for(AIMessage(content="ok"))
    async with c:
        one = app.state.runtimes.get("t1", str(tmp_path))
        one.set_plan(True)
        two = app.state.runtimes.get("t2", str(tmp_path))
        assert two.state.plan is False
        assert one.agent is not two.agent


async def test_second_turn_while_streaming_is_409(client_for, tmp_path):
    c, app = client_for(AIMessage(content="ok"))
    async with c:
        runtime = app.state.runtimes.get("t1", str(tmp_path))
        await runtime.lock.acquire()
        try:
            resp, _ = await _say(c, tmp_path, "hi")
        finally:
            runtime.lock.release()
    assert resp.status_code == 409 and resp.json() == {"error": "session_busy"}


async def test_new_message_after_abandoned_approval_is_accepted(client_for, tmp_path):
    c, app = client_for(AIMessage(content="ok"))
    async with c:
        runtime = app.state.runtimes.get("t1", str(tmp_path))
        runtime.phase = "turn"  # paused on an approval nobody will answer
        resp, events = await _say(c, tmp_path, "hi")
    assert resp.status_code == 200 and events[-1] == {"event": "turn_done"}


async def test_mutating_turn_with_failing_verify_streams_notices_and_fixup(
    client_for, tmp_path, monkeypatch
):
    from luna.turn import engine

    results = iter([(False, "boom"), (True, "")])
    monkeypatch.setattr(engine, "run_verify", lambda cmd, wd: next(results))
    c, _ = client_for(
        _write_call(),
        AIMessage(content="wrote it"),
        AIMessage(content="fixed it"),
        verify="make check",
        yolo=True,  # no approval pause: this test is about the post-turn pipeline
    )
    async with c:
        _, events = await _say(c, tmp_path, "write a file")
    notices = [e for e in events if e["event"] == "notice"]
    assert {"event": "notice", "level": "warn", "text": "verify failed\nboom"} in notices
    assert {"event": "notice", "level": "ok", "text": "✓ verify ok"} in notices
    texts = "".join(e.get("text", "") for e in events if e["event"] == "text_delta")
    assert "fixed it" in texts
    assert [e["event"] for e in events].count("turn_done") == 1


async def test_approve_after_runtime_loss_still_finishes(client_for, tmp_path):
    c, app = client_for(_write_call(), AIMessage(content="done"))
    async with c:
        # make the write_file call require approval (no yolo, no allow rule)
        _, first = await _say(c, tmp_path, "write")
        assert first[-1]["event"] == "approval_needed"
        app.state.runtimes._items.clear()  # server restart / eviction
        resp = await c.post(
            "/sessions/t1/approve",
            json={"decision": {"type": "approve"}, "workdir": str(tmp_path)},
        )
    assert _parse(resp.text)[-1] == {"event": "turn_done"}


async def test_pinned_files_reach_the_model(client_for, tmp_path):
    (tmp_path / "keep.md").write_text("PINNED-CONTENT\n")
    c, app = client_for(AIMessage(content="ok"))
    async with c:
        runtime = app.state.runtimes.get("t1", str(tmp_path))
        runtime.state.pinned.add("keep.md")
        await _say(c, tmp_path, "hi")
        messages = runtime.agent.get_state({"configurable": {"thread_id": "t1"}}).values["messages"]
    assert "PINNED-CONTENT" in messages[0].content
