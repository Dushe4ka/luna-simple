"""Session list/create endpoints — a thin HTTP wrapper over SessionIndex."""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime

from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.core.persistence import SessionIndex
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


async def list_sessions(request: Request) -> JSONResponse:
    """List sessions for a workdir, newest first."""
    workdir = request.query_params.get("workdir")
    if (error := trust_error(request, workdir)) is not None:
        return error
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
                    "group": session_group(r.updated),
                    "relative_time": relative_time(r.updated),
                }
                for r in rows
            ]
        }
    )


async def create_session(request: Request) -> JSONResponse:
    """Create a new session with a random thread_id."""
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    thread_id = uuid.uuid4().hex
    return JSONResponse({"thread_id": thread_id, "workdir": body.get("workdir", ".")})
