"""Turn endpoints: start runs in the AgentManager and stream their events.

Every turn — a streamed message or a background run — is started with
``AgentManager.start``. ``POST /messages`` only *subscribes* to that run, so a
client that disconnects (TUI closed, network drop) just unsubscribes: the
turn finishes, is logged, and its completion runs ``after_turn``.
Approvals are a plain JSON call (``POST /approve``); the open stream continues.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging

from luna_core.errors import LunaCoreError
from luna_core.messages import Image, message_from_dict
from luna_core.policy import Decision
from luna_core.tools.base import args_preview
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.config.providers import LunaConfigError
from luna.core.store import make_title
from luna.extensions.subagents import subagent_summaries
from luna.server.runtime import LIVE, relevel, runtime_for
from luna.server.trust import trust_error
from luna.turn import engine

_log = logging.getLogger(__name__)
_FINAL = ("turn_done", "error", "cancelled")
_MESSAGE_TYPES = ("user", "assistant", "tool_results")
#: Log record the server writes when a run finished with no client attached.
RUN_DONE = "run_done"


def _sse(obj: dict) -> dict:
    return {"data": json.dumps(obj, ensure_ascii=False)}


def _prepare(runtime, content: str):
    """Blocking part of a turn start (config files, @mentions, git): run in a thread."""
    try:
        names = {n for n, _ in subagent_summaries(runtime.workdir)}
    except LunaConfigError:
        names = set()
    prepared = engine.prepare_turn(
        runtime.state, content, workdir=runtime.workdir, subagent_names=names
    )
    engine.before_turn(runtime.state)
    return prepared


async def _start(request: Request, body: dict, session_id: str, follower=None):
    """Start a turn: the common part of /messages and /background.

    Returns ``(runtime, info)``, a ``JSONResponse`` refusal, or an ``str``
    error message when the turn could not be prepared. ``follower`` gets the
    session's out-of-turn events from before the run starts.
    """
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    runtime = await runtime_for(request, session_id, body.get("workdir", "."))
    if isinstance(runtime, JSONResponse):
        return runtime
    if follower is not None:
        follower.attach(runtime)
    registry = request.app.state.runtimes
    if runtime.lock.locked() or registry.running(session_id):
        return JSONResponse({"error": "session_busy"}, status_code=409)
    async with runtime.lock:  # a free lock is taken without yielding: no TOCTOU
        try:
            prepared = await asyncio.to_thread(_prepare, runtime, body["content"])
            bundle = await runtime.bundle()
        except Exception as exc:  # noqa: BLE001 - reported to the user, turn not started
            _log.exception("could not prepare a turn for %s", session_id)
            return f"Не удалось подготовить ход: {type(exc).__name__}: {exc}"
        images = prepared.images + [
            Image(i["media_type"], i["data"]) for i in body.get("images") or []
        ]
        runtime.reload_after_turn = bool(body.get("reload_after"))
        runtime.reload_requested = False
        if not runtime.session.title:
            runtime.session.log.set_title(session_id, make_title(prepared.title_line))
        try:
            info = request.app.state.manager.start(
                bundle.agent,
                runtime.session,
                prepared.text,
                images=images,
                label=make_title(prepared.title_line),
            )
        except LunaCoreError:
            return JSONResponse({"error": "session_busy"}, status_code=409)
    return runtime, info


def closing_event(info, events: list[dict]) -> dict | None:
    """Return a final event for a run that ended without one (an unexpected exception)."""
    if events and events[-1]["event"] in _FINAL:
        return None
    if info.status == "failed":
        return {"event": "error", "message": info.error or "the turn failed", "kind": "error"}
    return {"event": "cancelled"}


class _Follower:
    """One client's view of a session: run events plus out-of-turn events.

    Queue items are ``(is_run_event, event)``; only a run's own final event
    (or :func:`closing_event`, via :func:`end_followers`) ends a stream.
    """

    def __init__(self, request: Request, session_id: str) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self._session_id = session_id
        self._all = request.app.state.followers
        self._all.setdefault(session_id, set()).add(self)
        manager = request.app.state.manager
        self._unsubscribe = [manager.subscribe(session_id, self.run_event)]

    def run_event(self, event: dict) -> None:
        """Queue an event of the session's run."""
        self.queue.put_nowait((True, event))

    def _out_of_turn(self, event: dict) -> None:
        self.queue.put_nowait((False, event))

    def attach(self, runtime) -> None:
        """Also receive the session's out-of-turn events."""
        self._unsubscribe.append(runtime.listen(self._out_of_turn))

    def close(self) -> None:
        """Stop receiving (the run itself goes on); safe to call more than once."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        followers = self._all.get(self._session_id)
        if followers is not None:
            followers.discard(self)
            if not followers:
                del self._all[self._session_id]


def end_followers(app, info, events: list[dict]) -> None:
    """Close the streams of a run that ended without a final event."""
    event = closing_event(info, events)
    if event is None:
        return
    for follower in list(app.state.followers.get(info.session_id, ())):
        follower.run_event(event)


async def post_message(request: Request):
    """Run a turn and stream it (the run continues if the client disconnects)."""
    session_id = request.path_params["session_id"]
    body = await request.json()
    # Subscribe before the run starts so no event is missed.
    follower = _Follower(request, session_id)
    try:
        started = await _start(request, body, session_id, follower)
    except BaseException:
        follower.close()
        raise
    if isinstance(started, JSONResponse):
        follower.close()
        return started
    if isinstance(started, str):
        follower.close()

        async def failed():
            yield _sse({"event": "error", "message": started, "kind": "error"})

        return EventSourceResponse(failed())

    async def stream():
        try:
            while True:
                is_run, event = await follower.queue.get()
                yield _sse(relevel(event))
                if is_run and event["event"] in _FINAL:
                    return
        finally:
            follower.close()

    # ``background`` also runs when the client left before the stream's first
    # step (the generator's ``finally`` never runs then).
    return EventSourceResponse(stream(), background=BackgroundTask(follower.close))


async def post_background(request: Request) -> JSONResponse:
    """Run a turn detached from any client."""
    session_id = request.path_params["session_id"]
    started = await _start(request, await request.json(), session_id)
    if isinstance(started, JSONResponse):
        return started
    if isinstance(started, str):
        return JSONResponse({"error": started}, status_code=500)
    _runtime, info = started
    return JSONResponse(dataclasses.asdict(info))


async def get_events(request: Request):
    """Replay a run's buffered events, then follow it live.

    Out-of-turn approvals still waiting for a decision (background
    subagents) are sent after the replay; while a run is live, out-of-turn
    events are forwarded as they happen. Those carry no ``index``.
    """
    session_id = request.path_params["session_id"]
    try:
        after = int(request.query_params.get("after", -1))
    except ValueError:
        return JSONResponse({"error": "after must be an integer"}, status_code=400)
    manager = request.app.state.manager
    follower = _Follower(request, session_id)
    runtime = request.app.state.runtimes.peek(session_id)
    if runtime is not None:
        follower.attach(runtime)
    # one synchronous snapshot: the backlog and whether the run was live *then*
    backlog = manager.events(session_id, after)
    info = manager.get(session_id)
    live = info is not None and info.status in LIVE
    pending = runtime.pending_out_of_turn() if runtime is not None else []

    async def stream():
        try:
            last = after
            for event in backlog:
                last = event["index"]
                yield _sse(relevel(event))
                if event["event"] in _FINAL:
                    return
            for event in pending:
                yield _sse(event)
            if info is None:
                return
            if not live:
                if backlog and (closing := closing_event(info, backlog)) is not None:
                    yield _sse(closing)
                return
            # live at the snapshot: everything after it is in the follower's queue
            while True:
                is_run, event = await follower.queue.get()
                if is_run and "index" in event:
                    if event["index"] <= last:
                        continue
                    last = event["index"]
                yield _sse(relevel(event))
                if is_run and event["event"] in _FINAL:
                    return
        finally:
            follower.close()

    return EventSourceResponse(stream(), background=BackgroundTask(follower.close))


async def post_approve(request: Request) -> JSONResponse:
    """Answer a pending approval; the open stream continues."""
    session_id = request.path_params["session_id"]
    body = await request.json()
    runtime = request.app.state.runtimes.peek(session_id)
    if runtime is None:
        return JSONResponse({"error": "no running turn"}, status_code=404)
    try:
        call_id = body["call_id"]
        decision = Decision.from_dict(body["decision"])
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": f"bad decision: {exc}"}, status_code=400)
    try:
        runtime.session.decide(call_id, decision)
    except LunaCoreError as exc:  # nothing pending for that call
        return JSONResponse({"error": str(exc)}, status_code=404)
    runtime.answered(call_id)
    return JSONResponse({"ok": True})


async def post_cancel(request: Request) -> JSONResponse:
    """Cancel the running turn."""
    return JSONResponse(
        {"cancelled": request.app.state.manager.cancel(request.path_params["session_id"])}
    )


def _hidden_turns(records) -> set[str]:
    hidden: set[str] = set()
    for rec in records:
        if rec.type == "rewound":
            hidden.add(rec.payload["turn_id"])
        elif rec.type == "restored":
            hidden.discard(rec.payload["turn_id"])
    return hidden


def history_entries(records) -> list[dict]:
    """Build the transcript the TUI replays from a session's log records.

    User/assistant text, one row per finished tool call and the completion
    notice of a run that finished while no client watched. The whole
    conversation is shown (compaction only shortens the model's context);
    undone turns are not.
    """
    hidden = _hidden_turns(records)
    calls: dict[str, dict] = {}
    entries: list[dict] = []
    turn = None
    for rec in records:
        if rec.type == "turn_started":
            turn = rec.payload["turn_id"]
        if turn in hidden:
            continue
        if rec.type == RUN_DONE:
            entries.append(
                {"role": "notice", "level": rec.payload["level"], "text": rec.payload["text"]}
            )
            continue
        if rec.type not in _MESSAGE_TYPES:
            continue
        message = message_from_dict(rec.payload["message"])
        if message.role in ("user", "assistant"):
            if message.text:
                entries.append({"role": message.role, "content": message.text})
            for call in message.tool_calls:
                calls[call.id] = {"name": call.name, "args": call.args}
            continue
        for result in message.results:
            call = calls.get(result.call_id, {})
            text = result.text.strip()
            entries.append(
                {
                    "role": "tool",
                    "name": result.name or call.get("name", ""),
                    "args_preview": args_preview(call.get("args") or {}),
                    "ok": not result.is_error,
                    "detail": text.splitlines()[0][:120] if text else "",
                }
            )
    return entries


def completion_notice(info) -> tuple[str, str]:
    """``(level, text)`` announcing a finished run (shown when the session is reopened)."""
    label = info.label or "без названия"
    if info.status == "done":
        return "ok", f"фоновая сессия «{label}» завершена"
    if info.status == "failed":
        return "error", f"фоновая сессия «{label}» — ошибка: {info.error}"
    return "dim", f"фоновая сессия «{label}» прервана"


async def get_history(request: Request) -> JSONResponse:
    """Return the session's transcript, oldest first."""
    session_id = request.path_params["session_id"]
    workdir = request.query_params.get("workdir")
    if (error := trust_error(request, workdir)) is not None:
        return error
    runtime = await runtime_for(request, session_id, workdir or ".")
    if isinstance(runtime, JSONResponse):
        return runtime
    records = await asyncio.to_thread(runtime.session.records)
    return JSONResponse({"messages": history_entries(records)})


async def get_agents(request: Request) -> JSONResponse:
    """Background runs and background tasks of loaded sessions."""
    runs = [dataclasses.asdict(r) for r in request.app.state.manager.list()]
    tasks = [
        dataclasses.asdict(t)
        for runtime in request.app.state.runtimes.all()
        for t in runtime.background.list()
    ]
    return JSONResponse({"runs": runs, "tasks": tasks})
