import json

import httpx
import pytest
from luna_core.client import LunaClient, ServerError
from luna_core.policy import Decision


def sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


class Server:
    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append(
            (
                request.method,
                request.url.path,
                dict(request.url.params),
                body,
                request.headers.get("authorization"),
            )
        )
        path = request.url.path
        if path == "/sessions" and request.method == "POST":
            return httpx.Response(200, json={"id": "s1"})
        if path == "/sessions":
            return httpx.Response(200, json={"sessions": [{"id": "s1", "title": "t"}]})
        if path == "/sessions/s1/messages" and request.method == "POST":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse(
                    {"event": "text_delta", "text": "hi"},
                    {"event": "turn_done", "usage": {}, "cost": None, "stop_reason": "end"},
                ),
            )
        if path == "/sessions/s1/approve":
            return httpx.Response(200, json={"ok": True})
        if path == "/sessions/s1/cancel":
            return httpx.Response(200, json={"cancelled": True})
        if path == "/sessions/s9/messages":
            return httpx.Response(404, json={"error": "no session s9"})
        if path == "/agents":
            return httpx.Response(
                200, json={"runs": [{"session_id": "s1", "status": "running"}], "tasks": []}
            )
        return httpx.Response(500, json={"error": "unexpected"})


@pytest.fixture
def pair():
    server = Server()
    client = LunaClient(
        "http://luna",
        "tok",
        client=httpx.AsyncClient(base_url="http://luna", transport=httpx.MockTransport(server)),
    )
    return server, client


async def test_session_calls_and_auth(pair):
    server, client = pair
    assert await client.create_session("/w") == "s1"
    assert (await client.list_sessions("/w"))[0]["id"] == "s1"
    assert server.calls[0][1:] == ("/sessions", {}, {"workdir": "/w"}, "Bearer tok")
    assert server.calls[1][2] == {"workdir": "/w"}


async def test_send_streams_events_and_images(pair):
    server, client = pair
    events = [
        e
        async for e in client.send(
            "s1", "look", "/w", images=[{"media_type": "image/png", "data": "AAA"}]
        )
    ]
    assert [e["event"] for e in events] == ["text_delta", "turn_done"]
    assert server.calls[-1][3] == {
        "content": "look",
        "workdir": "/w",
        "images": [{"media_type": "image/png", "data": "AAA"}],
    }


async def test_decide_cancel_agents(pair):
    server, client = pair
    await client.decide("s1", "c1", Decision.edit({"command": "ls"}))
    assert server.calls[-1][3] == {
        "call_id": "c1",
        "decision": {"type": "edit", "args": {"command": "ls"}},
    }
    assert await client.cancel("s1") is True
    assert (await client.agents())["runs"][0]["status"] == "running"


async def test_errors_are_readable(pair):
    _server, client = pair
    with pytest.raises(ServerError, match="no session s9"):
        [e async for e in client.send("s9", "x", "/w")]
