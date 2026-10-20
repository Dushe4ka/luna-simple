"""Helpers for server tests: an app with scripted providers per session.

``httpx.ASGITransport`` buffers a whole response before returning it, so a
test could never answer an approval mid-stream or drop a stream early.
:class:`StreamingASGITransport` delivers body chunks as the app sends them
and tells the app ``http.disconnect`` when the response is closed — what a
real client (the TUI) does.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from luna_core.providers.fake import FakeProvider

from luna.server.app import create_app, shutdown
from luna.server.run import make_session_build


def make_app(
    scripts: dict[str, list] | None = None,
    default: list | None = None,
    *,
    trust_check=lambda workdir: True,
    built: list | None = None,
):
    """scripts: session id -> FakeProvider script (``default`` otherwise).

    ``built`` (optional) collects the id of every session bundle built.
    """

    def provider_factory(runtime):
        if built is not None:
            built.append(runtime.id)
        script = (scripts or {}).get(runtime.id, default or [FakeProvider.text("ok")])
        return FakeProvider(list(script))

    return create_app(
        token="t",
        build=make_session_build(provider_factory=provider_factory),
        trust_check=trust_check,
    )


class _Body(httpx.AsyncByteStream):
    def __init__(self, chunks: asyncio.Queue, task: asyncio.Task, gone: asyncio.Event) -> None:
        self._chunks = chunks
        self._task = task
        self._gone = gone

    async def __aiter__(self):
        while (chunk := await self._chunks.get()) is not None:
            if chunk:
                yield chunk

    async def aclose(self) -> None:
        self._gone.set()  # the client went away
        try:
            await asyncio.wait_for(asyncio.shield(self._task), 5)
        except TimeoutError:
            self._task.cancel()
        except asyncio.CancelledError:
            if not self._task.cancelled():
                raise


class StreamingASGITransport(httpx.AsyncBaseTransport):
    """An in-process ASGI transport that streams responses (see the module doc)."""

    def __init__(self, app) -> None:
        self.app = app

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = b"".join([part async for part in request.stream])
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(k.lower(), v) for (k, v) in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 123),
            "root_path": "",
        }
        loop = asyncio.get_running_loop()
        started: asyncio.Future = loop.create_future()
        chunks: asyncio.Queue = asyncio.Queue()
        gone = asyncio.Event()
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                started.set_result(message)
            elif message["type"] == "http.response.body":
                chunks.put_nowait(message.get("body", b""))
                if not message.get("more_body", False):
                    chunks.put_nowait(None)

        async def run():
            try:
                await self.app(scope, receive, send)
            except Exception as exc:  # surfaced to the caller, never left unretrieved
                if not started.done():
                    started.set_exception(exc)
            finally:
                if not started.done():
                    started.set_exception(RuntimeError("the app sent no response"))
                chunks.put_nowait(None)

        task = loop.create_task(run())
        start = await started
        return httpx.Response(
            start["status"], headers=start.get("headers", []), stream=_Body(chunks, task, gone)
        )


class _Client(httpx.AsyncClient):
    """An ``AsyncClient`` over the app that also shuts the app down on exit."""

    def __init__(self, app) -> None:
        super().__init__(
            transport=StreamingASGITransport(app),
            base_url="http://luna",
            headers={"Authorization": "Bearer t"},
        )
        self._app = app

    async def __aexit__(self, *exc) -> None:
        try:
            await shutdown(self._app)
        finally:
            await super().__aexit__(*exc)


def client(app) -> httpx.AsyncClient:
    """``async with client(app) as c`` — requests go to ``app`` in-process."""
    return _Client(app)


async def sse(resp) -> list[dict]:
    """Parse every ``data:`` line of a streamed response."""
    out = []
    async for line in resp.aiter_lines():
        if line.startswith("data:"):
            out.append(json.loads(line[5:]))
    return out


def strip_index(events: list[dict]) -> list[dict]:
    """Events without the replay ``index`` field (for exact comparisons)."""
    return [{k: v for k, v in e.items() if k != "index"} for e in events]


async def turn(c, sid: str, workdir, content: str, *, decide: dict | None = None, **extra):
    """Stream one message to ``sid`` and return its events.

    An ``approval_needed`` is answered with ``decide`` (the stream goes on),
    or ends the read when ``decide`` is None (the turn stays paused).
    """
    events: list[dict] = []
    body = {"workdir": str(workdir), "content": content, **extra}
    async with c.stream("POST", f"/sessions/{sid}/messages", json=body) as resp:
        if resp.status_code != 200:
            await resp.aread()
            return resp, events
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:])
            events.append(event)
            if event["event"] == "approval_needed":
                if decide is None:
                    break
                for request in event["value"]["action_requests"]:
                    answer = await c.post(
                        f"/sessions/{sid}/approve",
                        json={"call_id": request["call_id"], "decision": decide},
                    )
                    assert answer.json() == {"ok": True}
    return resp, events


def seed(log, workdir, title: str, *, session_id: str | None = None) -> str:
    """A logged conversation (one user message) in ``workdir``; returns its id."""
    from pathlib import Path

    from luna_core.messages import Message, message_to_dict

    sid = log.create(str(Path(workdir).resolve()), title=title, session_id=session_id)
    log.append(sid, "user", {"message": message_to_dict(Message.user(title))})
    return sid
