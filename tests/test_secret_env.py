"""Stored and shell API keys never reach ``execute`` commands (security group A1)."""

import os

import httpx
from luna_core.events import ToolFinished
from luna_core.providers.base import ModelRequest
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session

from luna.config.config import LunaConfig, load_config
from luna.config.credentials import set_api_key
from luna.config.providers import build_provider
from luna.core.agent import build_agent


def test_stored_key_is_not_exported_to_os_environ(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    set_api_key("deepseek", "sk-stored-deepseek")
    provider, _ = build_provider("deepseek", None)
    assert provider.api_key == "sk-stored-deepseek"
    assert "DEEPSEEK_API_KEY" not in os.environ


async def test_provider_authenticates_with_the_stored_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    set_api_key("deepseek", "sk-stored-deepseek")
    provider, model = build_provider("deepseek", None)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        chunk = b'{"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":"stop"}]}'
        body = b"data: " + chunk + b"\n\n"
        body += b"data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = ModelRequest(model=model, system="s", messages=[], tools=[])
    [event async for event in provider.stream(request)]
    assert seen[0].headers["Authorization"] == "Bearer sk-stored-deepseek"


async def _run_env(tmp_path, config):
    script = [FakeProvider.call("execute", {"command": "env"}), FakeProvider.text("ok")]
    provider = FakeProvider(script)
    bundle = await build_agent(config, provider=provider)
    try:
        session = Session.create(EventLog(tmp_path / "s.db"), str(tmp_path), undo=False)
        events = [e async for e in bundle.agent.run(session, "go")]
    finally:
        await bundle.aclose()
    assert any(isinstance(e, ToolFinished) for e in events), events
    return repr(provider.requests[-1].messages)  # what the model saw


async def test_execute_env_shows_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shell-openai")
    monkeypatch.setenv("LUNA_VISIBLE_MARKER", "marker-ok")
    set_api_key("deepseek", "sk-stored-deepseek")
    build_provider("deepseek", None)  # the real startup path reads the stored key
    output = await _run_env(tmp_path, LunaConfig(workdir=str(tmp_path), yolo=True))
    assert "marker-ok" in output
    assert "sk-stored-deepseek" not in output and "sk-shell-openai" not in output


async def test_execute_pass_env_lets_a_named_var_through(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-passed")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shell-openai")
    (tmp_path / ".luna.toml").write_text('[execute]\npass_env = ["GITHUB_TOKEN"]\n')
    config = load_config(cwd=str(tmp_path))
    assert config.execute_pass_env == ("GITHUB_TOKEN",)
    config.workdir, config.yolo = str(tmp_path), True
    output = await _run_env(tmp_path, config)
    assert "ghp-passed" in output and "sk-shell-openai" not in output
