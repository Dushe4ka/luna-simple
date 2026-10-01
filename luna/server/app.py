"""Starlette app: the local Luna server's ASGI entry point."""

from __future__ import annotations

from collections.abc import Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from luna.server.approvals import post_approve
from luna.server.commands import get_commands, get_state, post_command
from luna.server.runtime import RuntimeRegistry
from luna.server.sessions import create_session, list_sessions
from luna.server.turns import get_history, post_message


class _AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        header = request.headers.get("authorization", "")
        if header != f"Bearer {self._token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_app(
    agent_factory: Callable[[str], object] | None = None,
    *,
    token: str,
    trust_check: Callable[[str], bool] | None = None,
    session_agent_factory: Callable[[object], object] | None = None,
) -> Starlette:
    """Build the Starlette app.

    ``agent_factory(workdir)`` is stored on app.state for the route handlers
    to use. It takes the request's ``workdir`` because one server process
    serves many projects, and an agent's filesystem root is fixed at build
    time — see :func:`luna.server.run.run_serve`.

    ``session_agent_factory(runtime)`` builds one agent per session (production);
    the older per-workdir ``agent_factory(workdir)`` is kept for tests and is
    adapted to a session factory.

    ``trust_check(workdir)`` gates every workdir-scoped route (see
    :mod:`luna.server.trust`); ``None`` disables the check.
    """
    app = Starlette(
        routes=[
            Route("/health", _health),
            Route("/sessions", list_sessions, methods=["GET"]),
            Route("/sessions", create_session, methods=["POST"]),
            Route("/sessions/{thread_id}/messages", get_history, methods=["GET"]),
            Route("/sessions/{thread_id}/messages", post_message, methods=["POST"]),
            Route("/sessions/{thread_id}/approve", post_approve, methods=["POST"]),
            Route("/commands", get_commands, methods=["GET"]),
            Route("/sessions/{thread_id}/command", post_command, methods=["POST"]),
            Route("/sessions/{thread_id}/state", get_state, methods=["GET"]),
        ],
        middleware=[Middleware(_AuthMiddleware, token=token)],
    )
    if session_agent_factory is None:
        if agent_factory is None:
            raise TypeError("create_app needs agent_factory or session_agent_factory")

        def session_agent_factory(runtime):
            return agent_factory(runtime.workdir)

    app.state.agent_factory = agent_factory
    app.state.runtimes = RuntimeRegistry(session_agent_factory)
    app.state.trust_check = trust_check
    return app
