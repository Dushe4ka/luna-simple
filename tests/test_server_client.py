import httpx
import pytest
from luna_core.providers.fake import FakeProvider

from luna.server.client import ServerClient
from tests.server_helpers import StreamingASGITransport, make_app


def _client(app) -> ServerClient:
    client = ServerClient(base_url="http://test", token="t")
    client._http = httpx.AsyncClient(transport=StreamingASGITransport(app), base_url="http://test")
    return client


async def test_client_send_message_yields_parsed_events(tmp_path):
    client = _client(make_app(default=[FakeProvider.text("hi back")]))
    events = [e async for e in client.send_message("t1", "hi", str(tmp_path))]
    assert any(e["event"] == "text_delta" and e["text"] == "hi back" for e in events)
    assert events[-1]["event"] == "turn_done"


async def test_client_get_history_returns_prior_turns_in_the_tuis_roles(tmp_path):
    client = _client(make_app(default=[FakeProvider.text("hi back")]))
    async for _ in client.send_message("t1", "hi", str(tmp_path)):
        pass
    messages = await client.get_history("t1", str(tmp_path))
    assert messages == [{"role": "human", "content": "hi"}, {"role": "ai", "content": "hi back"}]


async def test_client_create_and_list_sessions(tmp_path):
    client = _client(make_app())
    thread_id = await client.create_session(str(tmp_path))
    assert thread_id
    async for _ in client.send_message(thread_id, "hi", str(tmp_path)):
        pass
    sessions = await client.list_sessions(str(tmp_path))
    assert [s["thread_id"] for s in sessions] == [thread_id]
    assert sessions[0]["id"] == thread_id


async def test_an_approval_ends_the_stream_and_approve_follows_the_same_run(tmp_path):
    app = make_app(
        default=[
            FakeProvider.call("write_file", {"file_path": "a.txt", "content": "x"}, "w1"),
            FakeProvider.text("written"),
        ]
    )
    client = _client(app)
    first = [e async for e in client.send_message("t1", "write", str(tmp_path))]
    assert first[-1]["event"] == "approval_needed"
    rest = [e async for e in client.approve("t1", {"type": "approve"}, str(tmp_path))]
    assert [e["event"] for e in rest][-1] == "turn_done"
    assert any(e["event"] == "tool_finished" and e["ok"] for e in rest)
    assert (tmp_path / "a.txt").read_text() == "x"
    assert not any(e["event"] in ("turn_started", "approval_needed") for e in rest)


async def test_approve_without_a_pending_request_is_a_readable_error(tmp_path):
    from luna.server.client import ServerError

    client = _client(make_app())
    with pytest.raises(ServerError, match="ожидающего подтверждения"):
        [e async for e in client.approve("t1", {"type": "approve"}, str(tmp_path))]


def _untrusted_client() -> ServerClient:
    return _client(make_app(trust_check=lambda _w: False))


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


async def test_a_dropped_plain_request_becomes_a_readable_server_error():
    from luna.server.client import ServerError

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = ServerClient(base_url="http://test", token="t")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    with pytest.raises(ServerError, match="Связь с сервером Luna"):
        await client.list_sessions("/repo")


async def test_session_busy_has_a_readable_message():
    from luna.server.client import ServerError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "session_busy"})

    client = ServerClient(base_url="http://test", token="t")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    with pytest.raises(ServerError, match="Дождитесь окончания ответа"):
        await client.run_command("t1", "/plan", "/repo")
