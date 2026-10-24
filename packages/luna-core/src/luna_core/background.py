"""Background work: subagent tasks inside a session, and whole detached sessions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from luna_core.errors import LunaCoreError
from luna_core.events import (
    BackgroundDone,
    Cancelled,
    NeedsApproval,
    TextDelta,
    TurnDone,
    TurnError,
    TurnStarted,
    to_wire,
)
from luna_core.session import Session

logger = logging.getLogger(__name__)

KEEP_FINISHED = 200  # finished tasks / runs kept for status and replay


def _require_loop(what: str) -> None:
    """Raise a clear error (before anything is registered) when no event loop is running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        raise LunaCoreError(f"cannot {what}: there is no running event loop") from None


@dataclass
class TaskInfo:
    """A background task inside a session."""

    handle: str
    label: str
    session_id: str
    status: str
    started: float
    finished: float | None = None
    ok: bool | None = None
    summary: str = ""


class BackgroundTasks:
    """Runs ``task(background=true)`` work and delivers results to the parent session."""

    def __init__(self, max_parallel: int = 4) -> None:
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._infos: dict[str, TaskInfo] = {}
        self._tasks: dict[str, asyncio.Task] = {}  # live tasks only
        self._done: OrderedDict[str, None] = OrderedDict()  # finished handles, oldest first

    def start(
        self, label: str, factory: Callable[[], Awaitable[object]], *, session: Session
    ) -> str:
        """Start ``factory()`` in the background; returns its handle."""
        _require_loop("start a background task")
        handle = f"bg-{uuid.uuid4().hex[:6]}"
        info = TaskInfo(handle, label, session.id, "queued", time.time())
        self._infos[handle] = info
        task = asyncio.create_task(self._run(info, factory, session))
        # A task cancelled before its first step never enters ``_run``; deliver then.
        task.add_done_callback(lambda _t: self._deliver(info, session))
        self._tasks[handle] = task
        return handle

    async def _run(
        self, info: TaskInfo, factory: Callable[[], Awaitable[object]], session: Session
    ) -> None:
        try:
            async with self._semaphore:
                info.status = "running"
                result = await factory()
            info.ok = bool(getattr(result, "ok", True))
            info.summary = result.render() if hasattr(result, "render") else str(result)
            info.status = "done" if info.ok else "failed"
        except asyncio.CancelledError:
            info.status, info.ok, info.summary = "cancelled", False, "cancelled"
        except Exception as exc:  # noqa: BLE001 - reported to the parent, never raised
            info.status, info.ok, info.summary = "failed", False, f"{type(exc).__name__}: {exc}"
        finally:
            self._deliver(info, session)

    def _deliver(self, info: TaskInfo, session: Session) -> None:
        """Log, queue and notify the result — exactly once per task."""
        if info.finished is not None:
            return
        handle = info.handle
        if info.status in ("queued", "running"):
            info.status, info.ok, info.summary = "cancelled", False, "cancelled"
        info.finished = time.time()
        payload = {"handle": handle, "ok": bool(info.ok), "summary": info.summary}
        try:
            session.append("background_done", payload)
        except Exception as exc:  # noqa: BLE001 - a closed log must not lose the delivery
            logger.warning("could not log background task %s: %r", handle, exc, exc_info=True)
        session.queue(f"[background task {handle} · {info.label} · {info.status}]\n{info.summary}")
        session.notify(BackgroundDone(handle, bool(info.ok), info.summary[:2000]))
        self._tasks.pop(handle, None)
        self._done[handle] = None
        while len(self._done) > KEEP_FINISHED:
            old, _ = self._done.popitem(last=False)
            self._infos.pop(old, None)

    def get(self, handle: str) -> TaskInfo | None:
        """One task's info."""
        return self._infos.get(handle)

    def list(self, session_id: str | None = None) -> list[TaskInfo]:
        """Tasks (of one session), oldest first."""
        return [i for i in self._infos.values() if session_id is None or i.session_id == session_id]

    def cancel(self, handle: str) -> bool:
        """Cancel a running or queued task."""
        task = self._tasks.get(handle)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def wait(self, handle: str) -> TaskInfo:
        """Wait until the task has finished.

        Raises ``LunaCoreError`` for an unknown handle, or one evicted after
        ``KEEP_FINISHED`` newer tasks finished.
        """
        info = self._infos.get(handle)
        if info is None:
            raise LunaCoreError(f"no task {handle}")
        task = self._tasks.get(handle)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return info

    async def shutdown(self) -> None:
        """Cancel everything."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@dataclass
class RunInfo:
    """A detached session run."""

    session_id: str
    label: str
    status: str
    started: float
    finished: float | None = None
    error: str = ""
    cost: float = 0.0
    text: str = ""
    #: ``"message"`` for a turn a client streamed (``POST /messages``), ``"background"``
    #: for a run started detached (``POST /background``).
    origin: str = "message"


class AgentManager:
    """Owns detached session runs (server side); clients subscribe and unsubscribe freely."""

    def __init__(
        self,
        max_parallel: int = 4,
        on_done: Callable[[RunInfo], None] | None = None,
        buffer: int = 5000,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._on_done = on_done
        self._limit = buffer
        self._runs: dict[str, RunInfo] = {}
        self._tasks: dict[str, asyncio.Task] = {}  # live runs only
        self._sessions: dict[str, Session] = {}  # live runs only
        self._events: dict[str, deque[dict]] = {}
        self._next_index: dict[str, int] = {}  # monotonic per session, across runs
        self._listeners: dict[str, list[Callable[[dict], None]]] = {}
        self._done: OrderedDict[str, None] = OrderedDict()  # finished session ids, oldest first

    def start(
        self,
        agent,
        session: Session,
        text: str,
        *,
        images=(),
        label: str = "",
        origin: str = "message",
    ) -> RunInfo:
        """Run one turn of ``session`` detached from any client."""
        _require_loop("start a session run")
        current = self._tasks.get(session.id)
        if (current and not current.done()) or session.busy:
            raise LunaCoreError(f"session {session.id} is already running")
        info = RunInfo(
            session.id, label or session.title or text[:60], "queued", time.time(), origin=origin
        )
        self._runs[session.id] = info
        self._sessions[session.id] = session
        self._events[session.id] = deque(maxlen=self._limit)
        self._next_index.setdefault(session.id, 0)
        self._done.pop(session.id, None)
        task = asyncio.create_task(self._run(agent, session, text, list(images), info))
        # A run cancelled before its first step never enters ``_run``; finish it then.
        task.add_done_callback(lambda _t: self._finish(session, info))
        self._tasks[session.id] = task
        return info

    def _publish(self, session_id: str, wire: dict) -> None:
        events = self._events[session_id]
        index = self._next_index[session_id]
        self._next_index[session_id] = index + 1
        wire = {**wire, "index": index}
        events.append(wire)
        for fn in list(self._listeners.get(session_id, [])):
            try:
                fn(wire)
            except Exception as exc:  # noqa: BLE001 - a broken client must not stop the run
                logger.warning(
                    "run listener for session %s failed: %r", session_id, exc, exc_info=True
                )

    async def _run(self, agent, session: Session, text: str, images: list, info: RunInfo) -> None:
        try:
            async with self._semaphore:
                info.status = "running"
                turn = agent.run(session, text, images=images)
                async with contextlib.aclosing(turn) as events:
                    async for event in events:
                        self._track(session, info, event)
                        self._publish(session.id, to_wire(event))
        except asyncio.CancelledError:
            info.status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - reported through RunInfo
            info.status, info.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self._finish(session, info)

    @staticmethod
    def _track(session: Session, info: RunInfo, event: object) -> None:
        if isinstance(event, NeedsApproval):
            info.status = "waiting"
        elif info.status == "waiting" and not session.pending_approvals:
            info.status = "running"
        if isinstance(event, TurnStarted):
            info.text = ""
        elif isinstance(event, TextDelta):
            info.text += event.text
        elif isinstance(event, TurnDone):
            info.status = "done"
        elif isinstance(event, TurnError):
            info.status, info.error = "failed", event.message
        elif isinstance(event, Cancelled):
            info.status = "cancelled"

    def _finish(self, session: Session, info: RunInfo) -> None:
        """Close the run: ``on_done`` and ``BackgroundDone`` — exactly once per run."""
        if info.finished is not None:
            return
        if info.status in ("queued", "running", "waiting"):
            info.status = "cancelled"
        info.finished = time.time()
        with contextlib.suppress(Exception):
            info.cost = session.cost()
        if self._on_done:
            try:
                self._on_done(info)
            except Exception as exc:  # noqa: BLE001 - a broken callback must not lose the notice
                logger.warning(
                    "on_done callback for session %s failed: %r", session.id, exc, exc_info=True
                )
        summary = info.text[-2000:] or info.error
        session.notify(BackgroundDone(session.id, info.status == "done", summary))
        if self._runs.get(session.id) is info:
            self._tasks.pop(session.id, None)
            self._sessions.pop(session.id, None)
            self._done[session.id] = None
            while len(self._done) > KEEP_FINISHED:
                old, _ = self._done.popitem(last=False)
                for store in (self._runs, self._events, self._next_index):
                    store.pop(old, None)
                if not self._listeners.get(old):
                    self._listeners.pop(old, None)

    def get(self, session_id: str) -> RunInfo | None:
        """Run info for a session."""
        return self._runs.get(session_id)

    def list(self) -> list[RunInfo]:
        """All runs, newest first."""
        return sorted(self._runs.values(), key=lambda r: r.started, reverse=True)

    def events(self, session_id: str, after: int = -1) -> list[dict]:
        """Buffered wire events with ``index > after`` (all by default)."""
        return [e for e in self._events.get(session_id, []) if e["index"] > after]

    def has_listeners(self, session_id: str) -> bool:
        """Return True while some client follows the run."""
        return bool(self._listeners.get(session_id))

    def subscribe(self, session_id: str, fn: Callable[[dict], None]) -> Callable[[], None]:
        """Receive live wire events; returns an unsubscribe callable."""
        self._listeners.setdefault(session_id, []).append(fn)

        def unsubscribe() -> None:
            listeners = self._listeners.get(session_id, [])
            if fn in listeners:
                listeners.remove(fn)

        return unsubscribe

    def cancel(self, session_id: str) -> bool:
        """Stop a run."""
        session = self._sessions.get(session_id)
        task = self._tasks.get(session_id)
        if session is None or task is None or task.done():
            return False
        if not session.cancel_turn():
            task.cancel()
        return True

    async def wait(self, session_id: str) -> RunInfo:
        """Wait for a run to finish.

        Raises ``LunaCoreError`` for a session that never ran here, or whose run
        was evicted after ``KEEP_FINISHED`` newer runs finished.
        """
        info = self._runs.get(session_id)
        if info is None:
            raise LunaCoreError(f"no run for session {session_id}")
        task = self._tasks.get(session_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return info

    async def shutdown(self) -> None:
        """Cancel every run (server shutdown)."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
