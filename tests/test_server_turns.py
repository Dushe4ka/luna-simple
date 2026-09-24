import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.server.app import create_app


@pytest.fixture
async def client(tmp_path, monkeypatch, fake_model):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    calls = [AIMessage(content="hello from the agent")]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    app = create_app(agent_factory=lambda: agent, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer secret-token"},
    ) as c:
        yield c, tmp_path


def _parse_sse(raw: str) -> list[dict]:
    events = []
    for block in raw.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


async def test_message_streams_text_delta_and_turn_done(client):
    c, tmp_path = client
    async with c.stream(
        "POST",
        "/sessions/abc/messages",
        json={"content": "hi", "workdir": str(tmp_path)},
    ) as resp:
        assert resp.status_code == 200
        raw = "".join([chunk async for chunk in resp.aiter_text()])
    events = _parse_sse(raw)
    assert {"event": "text_delta", "text": "hello from the agent"} in events
    assert {"event": "turn_done"} == events[-1]


async def test_message_records_session_title_from_first_message(client):
    from luna.core.persistence import SessionIndex

    c, tmp_path = client
    async with c.stream(
        "POST",
        "/sessions/abc/messages",
        json={"content": "please help me fix this bug", "workdir": str(tmp_path)},
    ) as resp:
        async for _ in resp.aiter_text():
            pass
    rows = SessionIndex().list(workdir=str(tmp_path))
    assert len(rows) == 1
    assert rows[0].thread_id == "abc"
    assert rows[0].title == "please help me fix this bug"
