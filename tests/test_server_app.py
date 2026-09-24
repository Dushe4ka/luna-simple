import httpx
import pytest

from luna.server.app import create_app
from luna.server.auth import read_token_file, token_path, write_token_file


@pytest.fixture
async def client():
    app = create_app(agent_factory=lambda _workdir: None, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer secret-token"},
    ) as c:
        yield c


async def test_health_ok_with_valid_token(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_health_rejects_missing_token():
    app = create_app(agent_factory=lambda _workdir: None, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/health")
    assert resp.status_code == 401


async def test_health_rejects_wrong_token():
    app = create_app(agent_factory=lambda _workdir: None, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": "Bearer wrong"}
    ) as c:
        resp = await c.get("/health")
    assert resp.status_code == 401


def test_write_and_read_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    write_token_file(port=54321, token="abc", pid=999)
    data = read_token_file()
    assert data == {"port": 54321, "token": "abc", "pid": 999}
    path = token_path()
    assert path.is_file()
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_read_token_file_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    assert read_token_file() is None
