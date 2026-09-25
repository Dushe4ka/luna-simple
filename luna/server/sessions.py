"""Session list/create endpoints — a thin HTTP wrapper over SessionIndex."""

from __future__ import annotations

import time
import uuid

from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.core.persistence import SessionIndex


def _relative_time(updated: float, *, now: float | None = None) -> str:
    """Render a Unix timestamp as a short relative label ("2м", "вчера", "18 сен")."""
    now = time.time() if now is None else now
    delta = max(0, now - updated)
    if delta < 60:
        return "сейчас"
    if delta < 3600:
        return f"{int(delta // 60)}м"
    if delta < 86400:
        return f"{int(delta // 3600)}ч"
    if delta < 172800:
        return "вчера"
    return time.strftime("%d %b", time.localtime(updated))


async def list_sessions(request: Request) -> JSONResponse:
    """List sessions for a workdir, newest first."""
    workdir = request.query_params.get("workdir")
    index = SessionIndex()
    rows = index.list(workdir=workdir)
    return JSONResponse(
        {
            "sessions": [
                {
                    "thread_id": r.thread_id,
                    "workdir": r.workdir,
                    "title": r.title,
                    "updated": r.updated,
                    "relative_time": _relative_time(r.updated),
                }
                for r in rows
            ]
        }
    )


async def create_session(request: Request) -> JSONResponse:
    """Create a new session with a random thread_id."""
    body = await request.json()
    thread_id = uuid.uuid4().hex
    return JSONResponse({"thread_id": thread_id, "workdir": body.get("workdir", ".")})
