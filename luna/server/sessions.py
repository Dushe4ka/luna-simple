"""Session endpoints over the core event log: list, create, delete."""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.core.store import conversations, descendants
from luna.server.trust import trust_error

_MONTHS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def session_group(updated: float, *, now: float | None = None) -> str:
    """Bucket a timestamp by local calendar day: today/yesterday/week/older.

    Calendar days, not 24-hour windows: 23:59 yesterday is "yesterday" even
    when it is only two minutes ago. A future timestamp (clock skew) counts
    as today.
    """
    now = time.time() if now is None else now
    days = (date.fromtimestamp(now) - date.fromtimestamp(updated)).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "week"
    return "older"


def relative_time(updated: float, *, now: float | None = None) -> str:
    """Short label matching the session's day group.

    "11м"/"3ч" today, "14:02" yesterday, "вт" this week, "25 сен" earlier.

    Russian month names come from a fixed table, not ``strftime("%b")``,
    which follows the process locale (it rendered "Sep").
    """
    now = time.time() if now is None else now
    group = session_group(updated, now=now)
    when = datetime.fromtimestamp(updated)
    if group == "today":
        delta = max(0.0, now - updated)
        if delta < 60:
            return "сейчас"
        if delta < 3600:
            return f"{int(delta // 60)}м"
        return f"{int(delta // 3600)}ч"
    if group == "yesterday":
        return when.strftime("%H:%M")
    if group == "week":
        return _WEEKDAYS[when.weekday()]
    return f"{when.day} {_MONTHS[when.month - 1]}"


def _conversations(log, workdir: str | None) -> list:
    """Top-level sessions that hold a conversation (``store.conversations`` semantics)."""
    return conversations(log, workdir)


async def list_sessions(request: Request) -> JSONResponse:
    """Sessions of a workdir, newest first.

    Subagent/background children and sessions nobody typed into yet are
    skipped (the CLI's ``-c`` and pickers skip them the same way).
    """
    workdir = request.query_params.get("workdir")
    if (error := trust_error(request, workdir)) is not None:
        return error
    resolved = str(Path(workdir).resolve()) if workdir else None
    manager = request.app.state.manager
    registry = request.app.state.runtimes
    rows = []
    for info in await asyncio.to_thread(_conversations, request.app.state.log, resolved):
        running = registry.running(info.id)
        rows.append(
            {
                "id": info.id,
                "title": info.title or "(new session)",
                "created": info.created,
                "updated": info.updated,
                "group": session_group(info.updated),
                "relative_time": relative_time(info.updated),
                "running": running,
                "background": running and not manager.has_listeners(info.id),
            }
        )
    return JSONResponse({"sessions": rows})


async def create_session(request: Request) -> JSONResponse:
    """Create an empty session in a workdir."""
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    workdir = str(Path(body.get("workdir") or ".").resolve())
    return JSONResponse({"id": request.app.state.log.create(workdir)})


def _busy(registry, session_id: str) -> bool:
    """Return whether ``session_id`` runs, is being deleted or has live background tasks."""
    if registry.running(session_id) or session_id in registry.deleting:
        return True
    runtime = registry.peek(session_id)
    return runtime is not None and (runtime.lock.locked() or runtime.has_live_tasks)


async def delete_session(request: Request) -> JSONResponse:
    """Delete a session and its subagent/background children.

    Refused while the session or any such child runs or has background
    tasks (the delete cascades, so a live child would lose its log). While
    they are deleted no runtime can be created for them, so no turn can
    start in a session whose log is about to go.
    """
    session_id = request.path_params["session_id"]
    registry = request.app.state.runtimes
    ids = [session_id, *descendants(request.app.state.log, session_id)]
    if any(_busy(registry, sid) for sid in ids):
        return JSONResponse({"error": "session_busy"}, status_code=409)
    registry.deleting.update(ids)
    try:
        for sid in ids:
            await registry.drop(sid)
        deleted = request.app.state.log.delete(session_id)
    finally:
        registry.deleting.difference_update(ids)
    return JSONResponse({"deleted": deleted})
