"""Turn endpoint: runs iter_turn and streams its events as SSE."""

from __future__ import annotations

import json

from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request

from luna.core.persistence import SessionIndex, make_title
from luna.core.turn_events import (
    Interrupted,
    ReloadRequested,
    TextDelta,
    ToolFinished,
    ToolStarted,
    UsageDelta,
    iter_turn,
)


def _event_dict(event) -> dict:
    if isinstance(event, TextDelta):
        return {"event": "text_delta", "text": event.text}
    if isinstance(event, UsageDelta):
        return {"event": "usage_delta", "usage_metadata": event.usage_metadata}
    if isinstance(event, ToolStarted):
        return {
            "event": "tool_started",
            "call_id": event.call_id,
            "name": event.name,
            "args": event.args,
        }
    if isinstance(event, ToolFinished):
        return {
            "event": "tool_finished",
            "call_id": event.call_id,
            "name": event.name,
            "ok": event.ok,
            "detail": event.detail,
        }
    if isinstance(event, ReloadRequested):
        return {"event": "reload_requested"}
    if isinstance(event, Interrupted):
        return {"event": "approval_needed", "value": event.value}
    raise TypeError(f"unknown turn event: {event!r}")


async def post_message(request: Request) -> EventSourceResponse:
    """Run one turn via iter_turn and stream its events as SSE."""
    thread_id = request.path_params["thread_id"]
    body = await request.json()
    content = body["content"]
    workdir = body.get("workdir", ".")
    agent = request.app.state.agent_factory()
    config = {"configurable": {"thread_id": thread_id}}
    payload = {"messages": [{"role": "user", "content": content}]}

    index = SessionIndex()
    existing = {r.thread_id for r in index.list(workdir=workdir)}
    if thread_id not in existing:
        index.record(thread_id, workdir, make_title(content))
    index.touch(thread_id)

    async def event_generator():
        interrupted = False
        for event in iter_turn(agent, payload, config):
            if isinstance(event, Interrupted):
                interrupted = True
            yield {"data": json.dumps(_event_dict(event))}
        if not interrupted:
            yield {"data": json.dumps({"event": "turn_done"})}

    return EventSourceResponse(event_generator())
