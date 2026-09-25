import httpx
import pytest
from langchain_core.messages import AIMessage

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.server.app import create_app
from luna.server.client import ServerClient


@pytest.fixture
def transport(tmp_path, monkeypatch, fake_model):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    calls = [AIMessage(content="hi back")]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    app = create_app(agent_factory=lambda _workdir: agent, token="secret-token")
    return httpx.ASGITransport(app=app), str(tmp_path)


async def test_client_send_message_yields_parsed_events(transport):
    asgi_transport, workdir = transport
    client = ServerClient(base_url="http://test", token="secret-token")
    client._http = httpx.AsyncClient(transport=asgi_transport, base_url="http://test")
    events = [e async for e in client.send_message("t1", "hi", workdir)]
    assert {"event": "text_delta", "text": "hi back"} in events
    assert events[-1] == {"event": "turn_done"}


async def test_client_create_and_list_sessions(transport):
    asgi_transport, workdir = transport
    client = ServerClient(base_url="http://test", token="secret-token")
    client._http = httpx.AsyncClient(transport=asgi_transport, base_url="http://test")
    thread_id = await client.create_session(workdir)
    assert thread_id
    async for _ in client.send_message(thread_id, "hi", workdir):
        pass
    sessions = await client.list_sessions(workdir)
    assert any(s["thread_id"] == thread_id for s in sessions)
