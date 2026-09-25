import httpx
import pytest

from luna.core.persistence import SessionIndex
from luna.server.app import create_app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    app = create_app(agent_factory=lambda _workdir: None, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


async def test_create_session_returns_a_thread_id(client):
    resp = await client.post("/sessions", json={"workdir": "/repo"})
    assert resp.status_code == 200
    data = resp.json()
    assert "thread_id" in data and len(data["thread_id"]) > 0


async def test_list_sessions_newest_first(client, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    index = SessionIndex()
    index.record("t1", "/repo", "first session")
    index.record("t2", "/repo", "second session")
    index.touch("t2")
    resp = await client.get("/sessions", params={"workdir": "/repo"})
    assert resp.status_code == 200
    rows = resp.json()["sessions"]
    assert [r["thread_id"] for r in rows] == ["t2", "t1"]
    assert rows[0]["title"] == "second session"
    assert "relative_time" in rows[0]
