"""Approval endpoint: resumes a turn paused on Interrupted."""

from __future__ import annotations

from langgraph.types import Command
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.core import permissions
from luna.core.persistence import SessionIndex
from luna.server.runtime import runtime_for
from luna.server.trust import trust_error
from luna.server.turns import stream_turn


async def post_approve(request: Request) -> EventSourceResponse | JSONResponse:
    """Resume a turn paused on ``Interrupted`` with the human's decision.

    Mirrors ``luna.core.session.collect_decisions``'s persist-then-strip
    handling of an ``"always"`` key: it's written as a project permission
    rule via :func:`luna.core.permissions.append_project_rule`, then
    removed before the decision is wrapped in ``Command(resume=...)`` —
    the graph's resume payload never expects that key.
    """
    thread_id = request.path_params["thread_id"]
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    workdir = body.get("workdir", ".")
    runtime = runtime_for(request, thread_id, workdir)
    if isinstance(runtime, JSONResponse):
        return runtime
    if runtime.lock.locked():
        return JSONResponse({"error": "session_busy"}, status_code=409)
    decision = dict(body["decision"])
    if "always" in decision:
        permissions.append_project_rule(runtime.workdir, decision["always"])
        decision.pop("always")
    if runtime.phase == "idle":
        runtime.phase = "turn"  # resumed after a restart/eviction: finish as a normal turn
    index = SessionIndex()
    index.touch(thread_id)
    payload = Command(resume={"decisions": [decision]})
    return EventSourceResponse(stream_turn(runtime, payload, index))
