"""Starlette app: the local Luna server's ASGI entry point (protocol v1 over luna-core)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import Awaitable, Callable

from luna_core.protocol import PROTOCOL_VERSION
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import luna
from luna.server.commands import get_commands, get_state, post_command
from luna.server.sessions import create_session, delete_session, list_sessions
from luna.server.turns import (
    RUN_DONE,
    completion_notice,
    end_followers,
    get_agents,
    get_events,
    get_history,
    post_approve,
    post_background,
    post_cancel,
    post_message,
)

_log = logging.getLogger(__name__)


class _AuthMiddleware:
    """Bearer-token check as pure ASGI middleware.

    Pure ASGI (not ``BaseHTTPMiddleware``) so a streamed response and the
    client's ``http.disconnect`` pass straight through to the SSE handlers.
    """

    def __init__(self, app, *, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            header = Headers(scope=scope).get("authorization", "").encode()
            if not hmac.compare_digest(header, self._expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "version": luna.__version__, "protocol": PROTOCOL_VERSION})


async def shutdown(app: Starlette) -> None:
    """Cancel every run, let completion handlers finish, close every runtime."""
    await app.state.manager.shutdown()
    while app.state.finishing:
        await asyncio.gather(*list(app.state.finishing), return_exceptions=True)
    await app.state.runtimes.aclose()


def create_app(
    *,
    token: str,
    build: Callable[[object], Awaitable],
    trust_check: Callable[[str], bool] | None = None,
    log=None,
) -> Starlette:
    """Build the Starlette app over luna-core.

    ``build(runtime)`` makes a session's agent bundle (see
    :func:`luna.server.run.make_session_build`). ``trust_check(workdir)``
    gates every workdir-scoped route (see :mod:`luna.server.trust`); ``None``
    disables the check. ``log`` is the event log (the app's
    ``sessions.db`` by default).
    """
    from luna_core.background import AgentManager

    from luna.core.store import open_log
    from luna.server.runtime import RuntimeRegistry

    log = log or open_log()

    async def finished(info, watched: bool, events: list[dict]) -> None:
        """Handle a finished run: completion notice, reload flags, ``after_turn``."""
        runtime = app.state.runtimes.peek(info.session_id)
        if not watched:
            # nobody watched the end: the notice is shown when the session is reopened
            level, text = completion_notice(info)
            with contextlib.suppress(Exception):
                log.append(info.session_id, RUN_DONE, {"level": level, "text": text})
        if runtime is None:
            return
        if any(e["event"] == "reload_requested" for e in events):
            runtime.reload_requested = True
        try:
            await runtime.after_turn()
        except Exception:  # noqa: BLE001 - a completion handler must never crash the server
            _log.exception("after_turn failed for %s", info.session_id)

    def on_done(info) -> None:
        # who is attached *now*, when the final event was just delivered
        watched = app.state.manager.has_listeners(info.session_id)
        events = app.state.manager.events(info.session_id)
        end_followers(app, info, events)
        task = asyncio.get_running_loop().create_task(finished(info, watched, events))
        app.state.finishing.add(task)
        task.add_done_callback(app.state.finishing.discard)

    manager = AgentManager(on_done=on_done)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        await shutdown(app)

    app = Starlette(
        routes=[
            Route("/health", _health),
            Route("/sessions", list_sessions, methods=["GET"]),
            Route("/sessions", create_session, methods=["POST"]),
            Route("/sessions/{session_id}", delete_session, methods=["DELETE"]),
            Route("/sessions/{session_id}/messages", get_history, methods=["GET"]),
            Route("/sessions/{session_id}/messages", post_message, methods=["POST"]),
            Route("/sessions/{session_id}/approve", post_approve, methods=["POST"]),
            Route("/sessions/{session_id}/cancel", post_cancel, methods=["POST"]),
            Route("/sessions/{session_id}/background", post_background, methods=["POST"]),
            Route("/sessions/{session_id}/events", get_events, methods=["GET"]),
            Route("/sessions/{session_id}/state", get_state, methods=["GET"]),
            Route("/sessions/{session_id}/command", post_command, methods=["POST"]),
            Route("/commands", get_commands, methods=["GET"]),
            Route("/agents", get_agents, methods=["GET"]),
        ],
        middleware=[Middleware(_AuthMiddleware, token=token)],
        lifespan=lifespan,
    )
    app.state.log = log
    app.state.manager = manager
    app.state.runtimes = RuntimeRegistry(build, log=log, manager=manager)
    app.state.trust_check = trust_check
    app.state.finishing = set()
    app.state.followers = {}
    return app
