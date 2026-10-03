"""Turn endpoint: runs iter_turn and streams its events as SSE."""

from __future__ import annotations

import functools
import json
import logging

from langgraph.types import Command
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.config.providers import LunaConfigError
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
    text_of,
    tool_outcome,
)
from luna.extensions.subagents import subagent_summaries
from luna.server.runtime import runtime_for
from luna.server.trust import trust_error
from luna.turn import engine


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


def _rule_decision(interrupt_value: dict, rules) -> dict | None:
    """Auto-answer an interrupt from project permission rules, else ``None``."""
    requests = interrupt_value.get("action_requests") or [interrupt_value.get("action_request")]
    request = requests[0] or {}
    name = request.get("action") or request.get("name")
    args = request.get("args", {}) or {}
    verdict = rules.match(name, args)
    if verdict == "allow":
        return {"type": "approve"}
    if verdict == "deny":
        return {"type": "reject", "message": f"blocked by a Luna permission rule ({name})"}
    return None


def _graph_events(agent, payload, config: dict, rules, outcome: engine.TurnOutcome):
    """Run the graph (sync; called in a worker thread) and yield SSE dicts.

    Yields ``("interrupt", value)`` last when the turn pauses for a human
    decision that no permission rule answers. Usage, tool names and reload
    requests are folded into ``outcome`` for :func:`engine.finish_turn`.
    """
    while True:
        interrupt_value = None
        for event in iter_turn(agent, payload, config):
            if isinstance(event, Interrupted):
                interrupt_value = event.value
                break
            if isinstance(event, UsageDelta):
                outcome.usage.merge(event.usage_metadata)
            elif isinstance(event, ToolFinished) and event.name:
                outcome.tool_names.add(event.name)
            elif isinstance(event, ReloadRequested):
                outcome.reload_requested = True
            yield _event_dict(event)
        if interrupt_value is None:
            return
        decision = _rule_decision(interrupt_value, rules)
        if decision is None:
            yield ("interrupt", interrupt_value)
            return
        payload = Command(resume={"decisions": [decision]})


def _sse(obj: dict) -> dict:
    return {"data": json.dumps(obj)}


def _notice(notice: engine.Notice) -> dict:
    return _sse({"event": "notice", "level": notice.level, "text": notice.text})


async def stream_turn(runtime, payload, index, *, prepare=None):
    """Stream a turn (or a resumed one) through the whole REPL-equivalent pipeline.

    Holds the session lock while streaming, so a second turn gets 409. The
    graph runs in a worker thread, keeping the event loop free for read-only
    commands mid-turn. When the graph finishes without a pending approval:
    ``finish_turn`` (usage, index, format/diagnose, verify), auto-reload, and
    at most one fix-up turn streamed in this same response, then
    ``finish_fixup``. ``turn_done`` is sent once, at the very end. With
    ``prepare`` (a fresh message) the turn is built under the lock and
    ``payload`` is ignored.
    """
    config = {"configurable": {"thread_id": runtime.thread_id}}
    rules = permissions.load_rules(runtime.workdir)
    async with runtime.lock:
        try:
            if prepare is not None:
                # Prepared only while holding the lock: a racing second message
                # must never overwrite this turn's PreparedTurn / outcome.
                runtime.prepared = await run_in_threadpool(prepare)
                runtime.phase = "turn"
                runtime.outcome = engine.TurnOutcome()
                payload = {"messages": [{"role": "user", "content": runtime.prepared.content}]}
            while True:
                paused = None
                events = _graph_events(runtime.agent, payload, config, rules, runtime.outcome)
                async for item in iterate_in_threadpool(events):
                    if isinstance(item, tuple):
                        paused = item[1]
                        break
                    yield _sse(item)
                if paused is not None:
                    yield _sse({"event": "approval_needed", "value": paused})
                    return
                cfg = await run_in_threadpool(runtime.config)
                if runtime.phase == "fixup":
                    for notice in await run_in_threadpool(engine.finish_fixup, cfg):
                        yield _notice(notice)
                else:
                    prepared = runtime.prepared or engine.PreparedTurn("", "", [])
                    result = await run_in_threadpool(
                        functools.partial(
                            engine.finish_turn,
                            runtime.state,
                            prepared,
                            runtime.outcome,
                            cfg=cfg,
                            index=index,
                            thread_id=runtime.thread_id,
                            workdir=runtime.workdir,
                        )
                    )
                    for notice in result.notices:
                        yield _notice(notice)
                    if result.reload or runtime.reload_after_turn:
                        runtime.reload_after_turn = False
                        try:
                            await run_in_threadpool(runtime.rebuild)
                            yield _notice(
                                engine.Notice("info", "auto-reloaded — new capabilities are live")
                            )
                        except Exception as exc:  # noqa: BLE001 - a bad config must not end the turn
                            yield _notice(engine.Notice("error", f"auto-reload failed: {exc}"))
                    if result.fixup_prompt is not None:
                        runtime.phase = "fixup"
                        runtime.outcome = engine.TurnOutcome()
                        payload = {"messages": [{"role": "user", "content": result.fixup_prompt}]}
                        continue
                runtime.finish()
                yield _sse({"event": "turn_done"})
                return
        except Exception as exc:
            logging.getLogger(__name__).exception("turn failed for thread %s", runtime.thread_id)
            runtime.finish()
            yield _sse({"event": "error", "message": f"{type(exc).__name__}: {exc}"})


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
            text = text_of(m.content)
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
    runtime = runtime_for(request, thread_id, raw_workdir or ".")
    if isinstance(runtime, JSONResponse):
        return runtime
    config = {"configurable": {"thread_id": thread_id}}

    def _read() -> list[dict]:
        # agent build + checkpoint read are blocking: keep them off the event loop
        raw_messages = runtime.agent.get_state(config).values.get("messages", [])
        return history_entries(raw_messages)

    messages = await run_in_threadpool(_read)
    return JSONResponse({"messages": messages})


async def post_message(request: Request) -> EventSourceResponse | JSONResponse:
    """Prepare a turn like the REPL does and stream it as SSE."""
    thread_id = request.path_params["thread_id"]
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    content = body["content"]
    workdir = body.get("workdir", ".")
    runtime = runtime_for(request, thread_id, workdir)
    if isinstance(runtime, JSONResponse):
        return runtime
    if runtime.lock.locked():
        return JSONResponse({"error": "session_busy"}, status_code=409)

    def _prepare():
        try:
            names = {n for n, _ in subagent_summaries(runtime.workdir)}
        except LunaConfigError:
            names = set()
        return engine.prepare_turn(
            runtime.state,
            content,
            agent=runtime.agent,
            thread_id=thread_id,
            workdir=runtime.workdir,
            session_id=thread_id,
            subagent_names=names,
        )

    index = SessionIndex()
    existing = {r.thread_id for r in index.list(workdir=runtime.workdir)}
    if thread_id not in existing:
        index.record(thread_id, runtime.workdir, make_title(content))
    index.touch(thread_id)
    return EventSourceResponse(stream_turn(runtime, None, index, prepare=_prepare))
