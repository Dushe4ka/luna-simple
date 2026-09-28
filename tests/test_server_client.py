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


async def test_client_get_history_returns_prior_turns(transport):
    asgi_transport, workdir = transport
    client = ServerClient(base_url="http://test", token="secret-token")
    client._http = httpx.AsyncClient(transport=asgi_transport, base_url="http://test")
    async for _ in client.send_message("t1", "hi", workdir):
        pass
    messages = await client.get_history("t1", workdir)
    assert {"role": "human", "content": "hi"} in messages
    assert {"role": "ai", "content": "hi back"} in messages


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


def _untrusted_client() -> ServerClient:
    app = create_app(agent_factory=lambda _w: None, token="t", trust_check=lambda _w: False)
    client = ServerClient(base_url="http://test", token="t")
    client._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client


async def test_http_errors_become_a_readable_server_error(tmp_path):
    from luna.server.client import ServerError

    client = _untrusted_client()
    with pytest.raises(ServerError, match="не отмечена как доверенная"):
        await client.list_sessions(str(tmp_path))


async def test_streaming_http_errors_become_a_readable_server_error(tmp_path):
    from luna.server.client import ServerError

    client = _untrusted_client()
    with pytest.raises(ServerError, match="не отмечена как доверенная"):
        [e async for e in client.send_message("t1", "hi", str(tmp_path))]


async def test_turn_stream_has_no_read_timeout():
    """Regression: the default 5 s read timeout killed any turn where the model
    was silent for >5 s (e.g. digesting web_search results); SSE pings only
    every 15 s, so a live turn looked dead to the client."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, text='data: {"event": "turn_done"}\n\n')

    client = ServerClient(base_url="http://test", token="t")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    [e async for e in client.send_message("t1", "hi", "/repo")]
    assert seen["timeout"]["read"] is None
    assert seen["timeout"]["connect"] == 5.0


async def test_a_dropped_stream_becomes_a_readable_server_error():
    from luna.server.client import ServerError

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    client = ServerClient(base_url="http://test", token="t")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    with pytest.raises(ServerError, match="Связь с сервером Luna"):
        [e async for e in client.send_message("t1", "hi", "/repo")]
