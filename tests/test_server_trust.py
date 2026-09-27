import httpx
import pytest

from luna.core.projects import ProjectIndex
from luna.server.app import create_app
from luna.server.run import make_trust_check


def _client(trust_check):
    app = create_app(agent_factory=lambda _w: None, token="t", trust_check=trust_check)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer t"},
    )


ROUTES = [
    ("GET", "/sessions", "params"),
    ("POST", "/sessions", "json"),
    ("GET", "/sessions/t1/messages", "params"),
    ("POST", "/sessions/t1/messages", "json"),
    ("POST", "/sessions/t1/approve", "json"),
]


@pytest.mark.parametrize(("method", "path", "where"), ROUTES)
async def test_untrusted_workdir_is_rejected_on_every_route(tmp_path, method, path, where):
    body = {"workdir": str(tmp_path), "content": "hi", "decision": {"type": "approve"}}
    kwargs = {"params": {"workdir": str(tmp_path)}} if where == "params" else {"json": body}
    async with _client(make_trust_check()) as c:
        resp = await c.request(method, path, **kwargs)
    assert resp.status_code == 403
    assert resp.json() == {"error": "workdir_not_trusted", "workdir": str(tmp_path)}


async def test_missing_workdir_is_a_400(tmp_path):
    async with _client(make_trust_check()) as c:
        resp = await c.get("/sessions")
    assert resp.status_code == 400
    assert resp.json() == {"error": "workdir_required"}


async def test_trust_granted_after_the_server_started_is_honoured(tmp_path):
    async with _client(make_trust_check()) as c:
        assert (await c.get("/sessions", params={"workdir": str(tmp_path)})).status_code == 403
        ProjectIndex().trust(str(tmp_path))  # e.g. a later `luna` launch in this folder
        assert (await c.get("/sessions", params={"workdir": str(tmp_path)})).status_code == 200


async def test_no_trust_check_means_no_enforcement(tmp_path):
    async with _client(None) as c:
        resp = await c.get("/sessions", params={"workdir": str(tmp_path)})
    assert resp.status_code == 200


def test_run_serve_wires_the_real_trust_check(tmp_path, monkeypatch):
    import uvicorn

    import luna.server.app as app_mod
    import luna.server.run as run_mod

    captured = {}
    real_create_app = app_mod.create_app

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_create_app(*args, **kwargs)

    monkeypatch.setattr(app_mod, "create_app", spy)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "write_token_file", lambda **k: None)

    assert run_mod.run_serve(["--port", "1", "--token", "x"]) == 0
    check = captured["trust_check"]
    assert check(str(tmp_path)) is False
    ProjectIndex().trust(str(tmp_path))
    assert check(str(tmp_path)) is True
