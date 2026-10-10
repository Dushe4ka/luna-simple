"""Helpers to fake provider HTTP endpoints in adapter tests."""

from __future__ import annotations

import json

import httpx


def sse_body(events: list[tuple[str | None, object]]) -> bytes:
    """Encode ``(event_name, data)`` pairs as an SSE stream body."""
    out = []
    for name, data in events:
        if name:
            out.append(f"event: {name}")
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        out.append(f"data: {payload}")
        out.append("")
    return ("\n".join(out) + "\n").encode()


class Recorder:
    """A MockTransport handler that records requests and replays responses in order."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    @property
    def last_json(self) -> dict:
        return json.loads(self.requests[-1].content)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def stream_response(events: list[tuple[str | None, object]], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": "text/event-stream"}, content=sse_body(events)
    )


def json_response(status: int, body: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})
