"""Turn endpoint: runs iter_turn and streams its events as SSE."""

from __future__ import annotations

import json
import logging

from langgraph.types import Command
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.core import permissions
from luna.core.persistence import SessionIndex, make_title
from luna.core.turn_events import (
    Interrupted,
    ReloadRequested,
    TextDelta,
    ToolFinished,
    ToolStarted,
    UsageDelta,
    args_preview,
    iter_turn,
    tool_outcome,
)
from luna.server.trust import trust_error


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
            "args_preview": args_preview(event.args),
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


async def _stream_turn_events(thread_id: str, workdir: str, agent, payload):
    """Run ``iter_turn`` and yield its events as SSE ``data`` dicts.

    Shared by :func:`post_message` and :mod:`luna.server.approvals`'s
    ``post_approve`` — both run one turn (a fresh message or a resume) and
    stream identically-shaped events. The session is touched unconditionally
    up front, since any message — including one that immediately pauses on
    an approval interrupt — counts as activity on that session; ``turn_done``
    fires only when the turn completes without pausing on another interrupt.

    Each interrupt is first checked against the project's permission rules,
    mirroring ``luna.core.session.collect_decisions``: an ``allow`` match
    auto-approves and a ``deny`` match auto-rejects, both without ever
    surfacing an ``approval_needed`` event to the client. Without this the
    rule the approval modal's "Always allow" button persists would be
    honoured by a later CLI/REPL session but never by the TUI that wrote it,
    so the user would be re-prompted for the identical action forever.
    """
    config = {"configurable": {"thread_id": thread_id}}
    index = SessionIndex()
    index.touch(thread_id)
    rules = permissions.load_rules(workdir)
    try:
        while True:
            interrupt_value = None
            for event in iter_turn(agent, payload, config):
                if isinstance(event, Interrupted):
                    interrupt_value = event.value
                    break
                yield {"data": json.dumps(_event_dict(event))}
            if interrupt_value is None:
                yield {"data": json.dumps({"event": "turn_done"})}
                return
            requests = interrupt_value.get("action_requests") or [
                interrupt_value.get("action_request")
            ]
            request = requests[0]
            name = request.get("action") or request.get("name")
            args = request.get("args", {}) or {}
            verdict = rules.match(name, args)
            if verdict == "allow":
                payload = Command(resume={"decisions": [{"type": "approve"}]})
                continue
            if verdict == "deny":
                payload = Command(
                    resume={
                        "decisions": [
                            {
                                "type": "reject",
                                "message": f"blocked by a Luna permission rule ({name})",
                            }
                        ]
                    }
                )
                continue
            yield {"data": json.dumps({"event": "approval_needed", "value": interrupt_value})}
            return
    except Exception as exc:
        # Without this, a turn that raises anywhere inside iter_turn (a
        # provider call failing, a tool erroring past its own guard) just
        # ends the SSE stream with zero bytes — indistinguishable, from the
        # client's side, from "the turn is still running". Logged too,
        # since the background-spawned server's stdout/stderr otherwise go
        # nowhere a person would ever see them.
        logging.getLogger(__name__).exception("turn failed for thread %s", thread_id)
        yield {"data": json.dumps({"event": "error", "message": f"{type(exc).__name__}: {exc}"})}


def _text_of(content) -> str:
    """Plain text of a message's content: a string, or the text blocks of a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block if isinstance(block, str) else block.get("text", "")
            for block in content
            if isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text")
        ]
        return "".join(parts)
    return ""


def history_entries(raw_messages: list) -> list[dict]:
    """Turn a thread's raw messages into the transcript the TUI replays.

    Human and AI text plus one entry per finished tool call, in order, with
    the same ``ok``/``detail`` the live ``tool_finished`` event carries
    (:func:`luna.core.turn_events.tool_outcome`). Durations are not stored,
    so replayed tool rows have none.
    """
    calls: dict[str, dict] = {}
    entries: list[dict] = []
    for m in raw_messages:
        kind = getattr(m, "type", None)
        if kind in ("human", "ai"):
            text = _text_of(m.content)
            if text:
                entries.append({"role": kind, "content": text})
            for call in getattr(m, "tool_calls", None) or []:
                if call.get("id"):
                    calls[call["id"]] = call
        elif kind == "tool":
            call = calls.get(getattr(m, "tool_call_id", None), {})
            ok, detail = tool_outcome(m)
            entries.append(
                {
                    "role": "tool",
                    "name": getattr(m, "name", None) or call.get("name", ""),
                    "args_preview": args_preview(call.get("args") or {}),
                    "ok": ok,
                    "detail": detail,
                }
            )
    return entries


async def get_history(request: Request) -> JSONResponse:
    """Return the thread's prior turns, oldest first.

    Includes finished tool calls (see :func:`history_entries`) so a replayed
    session shows the same inline tool rows the live transcript did.
    A thread the agent has never seen resolves to an empty snapshot (same
    behaviour ``session.py``'s resume recap already relies on), not an
    error, so a brand-new session's history is just ``[]``.
    """
    thread_id = request.path_params["thread_id"]
    raw_workdir = request.query_params.get("workdir")
    if (error := trust_error(request, raw_workdir)) is not None:
        return error
    workdir = raw_workdir or "."
    agent = request.app.state.agent_factory(workdir)
    config = {"configurable": {"thread_id": thread_id}}
    raw_messages = agent.get_state(config).values.get("messages", [])
    messages = history_entries(raw_messages)
    return JSONResponse({"messages": messages})


async def post_message(request: Request) -> EventSourceResponse | JSONResponse:
    """Run one turn via iter_turn and stream its events as SSE."""
    thread_id = request.path_params["thread_id"]
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    content = body["content"]
    workdir = body.get("workdir", ".")
    agent = request.app.state.agent_factory(workdir)
    payload = {"messages": [{"role": "user", "content": content}]}

    index = SessionIndex()
    existing = {r.thread_id for r in index.list(workdir=workdir)}
    if thread_id not in existing:
        index.record(thread_id, workdir, make_title(content))

    return EventSourceResponse(_stream_turn_events(thread_id, workdir, agent, payload))
