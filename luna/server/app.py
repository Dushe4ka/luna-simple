"""Starlette app: the local Luna server's ASGI entry point."""

from __future__ import annotations

from collections.abc import Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from luna.server.sessions import create_session, list_sessions
from luna.server.turns import post_message


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


def create_app(agent_factory: Callable[[], object], *, token: str) -> Starlette:
    """Build the Starlette app.

    ``agent_factory`` is stored on app.state for later tasks' route
    handlers to use.
    """
    app = Starlette(
        routes=[
            Route("/health", _health),
            Route("/sessions", list_sessions, methods=["GET"]),
            Route("/sessions", create_session, methods=["POST"]),
            Route("/sessions/{thread_id}/messages", post_message, methods=["POST"]),
        ],
        middleware=[Middleware(_AuthMiddleware, token=token)],
    )
    app.state.agent_factory = agent_factory
    return app
