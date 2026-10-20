import pytest

from luna.core.projects import ProjectIndex
from luna.server.run import make_trust_check
from tests.server_helpers import client, make_app


def _client(trust_check):
    return client(make_app(trust_check=trust_check))


ROUTES = [
    ("GET", "/sessions", "params"),
    ("POST", "/sessions", "json"),
    ("GET", "/sessions/t1/messages", "params"),
    ("POST", "/sessions/t1/messages", "json"),
    ("POST", "/sessions/t1/background", "json"),
    ("GET", "/sessions/t1/state", "params"),
    ("POST", "/sessions/t1/command", "json"),
    ("GET", "/commands", "params"),
]


@pytest.mark.parametrize(("method", "path", "where"), ROUTES)
async def test_untrusted_workdir_is_rejected_on_every_route(tmp_path, method, path, where):
    body = {"workdir": str(tmp_path), "content": "hi", "line": "/plan"}
    kwargs = {"params": {"workdir": str(tmp_path)}} if where == "params" else {"json": body}
    async with _client(make_trust_check()) as c:
        resp = await c.request(method, path, **kwargs)
        loaded = c._app.state.runtimes.peek("t1")
    assert resp.status_code == 403
    assert resp.json() == {"error": "workdir_not_trusted", "workdir": str(tmp_path)}
    assert loaded is None  # no agent is ever built for an untrusted folder


async def test_session_routes_without_a_workdir_need_a_loaded_session(tmp_path):
    async with _client(make_trust_check()) as c:
        approve = await c.post(
            "/sessions/t1/approve", json={"call_id": "c1", "decision": {"type": "approve"}}
        )
        cancel = await c.post("/sessions/t1/cancel")
        async with c.stream("GET", "/sessions/t1/events") as resp:
            events = [line async for line in resp.aiter_lines() if line.startswith("data:")]
    assert approve.status_code == 404
    assert cancel.json() == {"cancelled": False}
    assert events == []


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
    assert captured["build"] is not None
    check = captured["trust_check"]
    assert check(str(tmp_path)) is False
    ProjectIndex().trust(str(tmp_path))
    assert check(str(tmp_path)) is True


async def test_relative_workdir_is_rejected_not_resolved_against_the_servers_cwd(tmp_path):
    """The server is shared by every project: "." would mean *its* cwd, i.e.
    whichever project happened to start it — mixing projects' sessions."""
    ProjectIndex().trust(str(tmp_path))
    async with _client(make_trust_check()) as c:
        resp = await c.get("/sessions", params={"workdir": "."})
    assert resp.status_code == 400
    assert resp.json() == {"error": "workdir_not_absolute", "workdir": "."}
