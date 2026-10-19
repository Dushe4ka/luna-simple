"""Server-side state of each session: core session, app state, agent bundle.

The server is shared by every project and every open session. Each session
gets its own agent (its own plan flag, pipeline hooks and background tasks),
rebuilt only for that session on ``/model``, ``/provider`` or ``/reload``.
Turns run in the app's :class:`~luna_core.background.AgentManager`; this
module only owns the per-session state around them.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path

from luna_core.background import AgentManager, BackgroundTasks
from luna_core.events import NeedsApproval, Notice, to_wire
from luna_core.session import EventLog, Session
from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.config.config import LunaConfig, load_config
from luna.config.providers import LunaConfigError
from luna.core.session_state import SessionState, SessionStateStore
from luna.turn import engine

#: Run states in which a session counts as running.
LIVE = ("queued", "running", "waiting")


class WorkdirMismatch(Exception):
    """A session id was used with a different project folder than its own."""


class SessionDeleting(Exception):
    """The session is being deleted: no runtime may be created for it meanwhile."""


def relevel(event: dict) -> dict:
    """Give an ``info`` notice the level its leading marker asks for (hook notices)."""
    if event.get("event") == "notice" and event.get("level") == "info":
        return {**event, "level": engine.notice_level(event["text"])}
    return event


class SessionRuntime:
    """Live server-side state of one session.

    Out-of-turn events (``session.notify``: background-task completions,
    approvals asked by a background subagent) are not part of any run's
    replay buffer, so the runtime subscribes to them and fans them out to
    :meth:`listen` callbacks; a pending out-of-turn approval is kept until
    it is answered so a client that attaches later still sees it.
    """

    def __init__(
        self,
        state: SessionState,
        session: Session,
        *,
        store: SessionStateStore,
        build: Callable[[SessionRuntime], Awaitable],
    ) -> None:
        """Wrap ``session`` and its app ``state``; the agent is built on first use."""
        self.state = state
        self.session = session
        self._store = store
        self._build = build
        self._bundle = None
        self._bundle_lock = asyncio.Lock()
        self.lock = asyncio.Lock()
        self.background = BackgroundTasks()
        #: Set by ``reload_after`` on a message (``/init``): rebuild after that turn.
        self.reload_after_turn = False
        #: Set when a run emitted ``reload_requested``: rebuild after that turn.
        self.reload_requested = False
        self._listeners: list[Callable[[dict], None]] = []
        self._pending: dict[str, dict] = {}
        self._unsubscribe = session.subscribe(self._on_notify)

    @property
    def id(self) -> str:
        """The session id."""
        return self.session.id

    @property
    def workdir(self) -> str:
        """The session's project folder (absolute)."""
        return self.state.workdir

    async def bundle(self):
        """Return the session's agent bundle, built on first use."""
        async with self._bundle_lock:
            if self._bundle is None:
                self._bundle = await self._build(self)
            return self._bundle

    def config(self) -> LunaConfig:
        """Project config with this session's provider/model applied."""
        try:
            cfg = load_config({"workdir": self.workdir}, cwd=self.workdir)
        except (LunaConfigError, OSError):
            cfg = LunaConfig(workdir=self.workdir)
        if self.state.provider is not None:
            cfg.provider, cfg.model = self.state.provider, self.state.model
        elif self.state.model is not None:
            cfg.model = self.state.model
        return cfg

    async def rebuild(self) -> None:
        """Rebuild the agent (raises on a bad config; the old one stays then)."""
        async with self._bundle_lock:
            new = await self._build(self)
            old, self._bundle = self._bundle, new
        if old is not None:
            await old.aclose()

    async def _switch(self, provider: str | None, model: str | None) -> None:
        previous = (self.state.provider, self.state.model)
        self.state.provider, self.state.model = provider, model
        try:
            await self.rebuild()
        except Exception:
            self.state.provider, self.state.model = previous
            raise
        self.save()

    async def switch_model(self, model: str) -> None:
        """Use ``model`` for this session only; restores the old one on failure."""
        await self._switch(self.state.provider, model)

    async def switch_provider(self, provider: str) -> None:
        """Use ``provider`` (its default model) for this session only."""
        await self._switch(provider, None)

    def set_plan(self, on: bool) -> None:
        """Toggle plan mode (read by the policy on every tool call)."""
        self.state.plan = on
        self.save()

    def save(self) -> None:
        """Persist the app-level session state."""
        self._store.save(self.state)

    # -- out-of-turn events -------------------------------------------------
    def _on_notify(self, event: object) -> None:
        wire = relevel(to_wire(event))
        if isinstance(event, NeedsApproval):
            self._pending[event.request.call_id] = wire
        for fn in list(self._listeners):
            try:
                fn(wire)
            except Exception:  # noqa: BLE001 - a broken client must not stop the others
                pass

    def listen(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        """Receive out-of-turn wire events; returns an unsubscribe callable."""
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def pending_out_of_turn(self) -> list[dict]:
        """``approval_needed`` events of background subagents still waiting for a decision."""
        waiting = set(self.session.pending_approvals)
        for call_id in list(self._pending):
            # children list their ids unprefixed; the wire id is "<child>:<call>"
            if call_id not in waiting and call_id.partition(":")[2] not in waiting:
                del self._pending[call_id]
        return list(self._pending.values())

    def answered(self, call_id: str) -> None:
        """Forget an out-of-turn approval once it was decided."""
        self._pending.pop(call_id, None)

    @property
    def has_live_tasks(self) -> bool:
        """True while a ``task(background=true)`` of this session runs."""
        return any(t.status in ("queued", "running") for t in self.background.list())

    async def after_turn(self) -> None:
        """After a run finished: persist state and auto-reload when asked."""
        self.save()
        if self.reload_requested or self.reload_after_turn:
            self.reload_requested = self.reload_after_turn = False
            try:
                await self.rebuild()
            except Exception as exc:  # noqa: BLE001 - reported as an out-of-turn notice
                self.session.notify(Notice("error", f"auto-reload failed: {exc}"))
            else:
                self.session.notify(Notice("info", "auto-reloaded — new capabilities are live"))

    async def aclose(self) -> None:
        """Stop background tasks and close the agent's resources."""
        self._unsubscribe()
        await self.background.shutdown()
        if self._bundle is not None:
            await self._bundle.aclose()
            self._bundle = None


class RuntimeRegistry:
    """LRU of session runtimes; busy sessions are never evicted."""

    def __init__(
        self,
        build: Callable[[SessionRuntime], Awaitable],
        *,
        log: EventLog,
        manager: AgentManager,
        capacity: int = 8,
    ) -> None:
        """``build(runtime)`` makes a session's agent bundle."""
        self._build = build
        self.log = log
        self.manager = manager
        self._capacity = capacity
        self._items: OrderedDict[str, SessionRuntime] = OrderedDict()
        self._store = SessionStateStore()
        #: sessions whose finished run still has to run ``after_turn``
        self.finishing: set[str] = set()
        #: sessions being deleted (``get`` refuses them)
        self.deleting: set[str] = set()

    def peek(self, session_id: str) -> SessionRuntime | None:
        """Return the runtime if it is loaded."""
        return self._items.get(session_id)

    def all(self) -> list[SessionRuntime]:
        """Return the loaded runtimes."""
        return list(self._items.values())

    def running(self, session_id: str) -> bool:
        """Return True while a turn of ``session_id`` runs (or waits for approval)."""
        info = self.manager.get(session_id)
        runtime = self._items.get(session_id)
        return (info is not None and info.status in LIVE) or (
            runtime is not None and runtime.session.busy
        )

    def _evictable(self, runtime: SessionRuntime) -> bool:
        return not (
            self.running(runtime.id)
            or runtime.id in self.finishing
            or runtime.lock.locked()
            or runtime.has_live_tasks
            or runtime.session.pending_approvals
        )

    async def get(self, session_id: str, workdir: str) -> SessionRuntime:
        """Return the runtime for ``session_id`` (session created with that id when unknown)."""
        if session_id in self.deleting:
            raise SessionDeleting(session_id)
        resolved = str(Path(workdir).resolve())
        runtime = self._items.get(session_id)
        if runtime is not None:
            if runtime.workdir != resolved:
                raise WorkdirMismatch(session_id)
            self._items.move_to_end(session_id)
            return runtime
        info = self.log.info(session_id)
        if info is None:
            self.log.create(resolved, session_id=session_id)
        elif info.workdir != resolved:
            raise WorkdirMismatch(session_id)
        state = self._store.load(session_id, resolved)
        runtime = SessionRuntime(
            state, Session.open(self.log, session_id), store=self._store, build=self._build
        )
        self._items[session_id] = runtime
        for key in list(self._items):
            if len(self._items) <= self._capacity:
                break
            candidate = self._items[key]
            if key != session_id and self._evictable(candidate):
                del self._items[key]
                await candidate.aclose()
        return runtime

    async def drop(self, session_id: str) -> None:
        """Forget and close a runtime."""
        runtime = self._items.pop(session_id, None)
        if runtime is not None:
            await runtime.aclose()

    async def aclose(self) -> None:
        """Close every runtime."""
        for runtime in list(self._items.values()):
            await runtime.aclose()
        self._items.clear()


async def runtime_for(
    request: Request, session_id: str, workdir: str
) -> SessionRuntime | JSONResponse:
    """Return the request's runtime, or a 400 when the folder does not match."""
    try:
        return await request.app.state.runtimes.get(session_id, workdir)
    except WorkdirMismatch:
        return JSONResponse({"error": "workdir_mismatch"}, status_code=400)
    except SessionDeleting:
        return JSONResponse({"error": "session_busy"}, status_code=409)
