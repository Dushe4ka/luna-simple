"""``POST /credentials``: store an API key out of band (never as command text)."""

import json
import logging
import os
import stat

import httpx
import pytest

from luna.config.credentials import credentials_path, get_api_key
from luna.config.providers import PROVIDERS
from tests.server_helpers import StreamingASGITransport, client, make_app

SECRET = "sk-ant-TOPSECRET-4242"


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch):
    for spec in PROVIDERS.values():
        if spec.env_var:
            monkeypatch.delenv(spec.env_var, raising=False)


async def _set(c, wd, provider="anthropic", key=SECRET):
    return await c.post("/credentials", json={"workdir": wd, "provider": provider, "key": key})


async def test_requires_the_bearer_token(tmp_path):
    app = make_app()
    async with httpx.AsyncClient(
        transport=StreamingASGITransport(app), base_url="http://luna"
    ) as c:
        resp = await _set(c, str(tmp_path))
    assert resp.status_code == 401
    assert not credentials_path().exists()


async def test_stores_the_key_0600_stripped(tmp_path):
    async with client(make_app()) as c:
        resp = await _set(c, str(tmp_path), key=f"  {SECRET}\n")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "provider": "anthropic"}
    assert get_api_key("anthropic") == SECRET
    assert stat.S_IMODE(credentials_path().stat().st_mode) == 0o600
    assert SECRET not in os.environ.values()


@pytest.mark.parametrize(
    ("provider", "key", "error"),
    [
        ("nope", SECRET, "unknown_provider"),
        ("ollama", SECRET, "key_not_needed"),
        ("anthropic", "   ", "empty_key"),
        ("anthropic", "sk-a\nb", "bad_key"),
        ("anthropic", "sk a", "bad_key"),
        ("anthropic", "sk-\ud800", "bad_key"),
        ("anthropic", None, "bad_request"),
        (None, SECRET, "bad_request"),
    ],
)
async def test_rejects_bad_input_with_4xx(tmp_path, provider, key, error):
    async with client(make_app()) as c:
        body = {"workdir": str(tmp_path), "provider": provider, "key": key}
        # json.dumps escapes a lone surrogate as \\ud800, like any JSON client would
        resp = await c.post("/credentials", content=json.dumps(body).encode())
    assert resp.status_code == 400
    assert resp.json() == {"error": error}
    assert SECRET not in resp.text
    assert not credentials_path().exists()


async def test_malformed_body_is_400(tmp_path):
    async with client(make_app()) as c:
        resp = await c.post("/credentials", content=b"{not json " + SECRET.encode())
    assert resp.status_code == 400
    assert SECRET not in resp.text


async def test_untrusted_workdir_is_refused(tmp_path):
    async with client(make_app(trust_check=lambda wd: False)) as c:
        resp = await _set(c, str(tmp_path))
    assert resp.status_code == 403
    assert not credentials_path().exists()


async def test_custom_provider_from_the_project_config(tmp_path):
    (tmp_path / ".luna.toml").write_text(
        '[provider.custom.mylocal]\nenv_var = "MYLOCAL_API_KEY"\n'
        'base_url = "http://localhost:1/v1"\n'
    )
    async with client(make_app()) as c:
        resp = await _set(c, str(tmp_path), provider="mylocal")
    assert resp.status_code == 200, resp.text
    assert get_api_key("mylocal") == SECRET


async def test_key_then_switch_never_leaks_into_logs_events_or_history(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    wd = str(tmp_path)
    async with client(make_app()) as c:
        first = await c.post(
            "/sessions/t1/command", json={"workdir": wd, "line": "/provider anthropic"}
        )
        assert first.json()["effects"]["need_key"]["provider"] == "anthropic"
        assert (await _set(c, wd)).status_code == 200
        second = await c.post(
            "/sessions/t1/command", json={"workdir": wd, "line": "/provider anthropic"}
        )
        assert second.json()["effects"] == {"provider": "anthropic"}
        state = (await c.get("/sessions/t1/state", params={"workdir": wd})).json()
        history = await c.get("/sessions/t1/messages", params={"workdir": wd})
    assert state["provider"] == "anthropic"
    assert SECRET not in history.text
    assert SECRET not in caplog.text
    config_home = credentials_path().parent
    for path in config_home.rglob("*"):
        if path.is_file() and path.name != "credentials.toml":
            assert SECRET.encode() not in path.read_bytes(), path


async def test_oversized_body_is_refused_unread(tmp_path):
    async with client(make_app()) as c:
        resp = await _set(c, str(tmp_path), key="k" * 9000)
    assert resp.status_code == 413
    assert resp.json() == {"error": "too_large"}
    assert not credentials_path().exists()


async def test_an_unexpected_storage_failure_is_500_without_the_key(tmp_path, monkeypatch):
    from luna.server import credentials as route

    def boom(*a, **kw):
        raise RuntimeError(f"boom {SECRET}")

    monkeypatch.setattr(route, "set_api_key", boom)
    async with client(make_app()) as c:
        resp = await _set(c, str(tmp_path))
    assert resp.status_code == 500
    assert resp.json() == {"error": "cannot_store"}
