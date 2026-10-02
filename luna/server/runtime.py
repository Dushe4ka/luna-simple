"""One runtime per conversation: its own agent, settings, lock and turn phase.

The server is shared by every project and every open session. An agent's
undo journal (``session_id``), plan-mode flag and edit-anchor tracker are
fixed when it is built, so each session gets its own agent (as Hermes does:
"a fresh AIAgent per session"), rebuilt only for that session on
``/model``, ``/provider`` or ``/reload``.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.config.config import LunaConfig, load_config
from luna.config.providers import LunaConfigError
from luna.core.session_state import SessionState, SessionStateStore
from luna.turn.engine import PreparedTurn, TurnOutcome


class WorkdirMismatch(Exception):
    """A session id was used with a different project folder than its own."""


class SessionRuntime:
    """Live server-side state of one session."""

    def __init__(
        self,
        state: SessionState,
        *,
        store: SessionStateStore,
        build_agent: Callable[[SessionRuntime], object],
    ) -> None:
        self.state = state
        self._store = store
        self._build_agent = build_agent
        self._agent: object | None = None
        self.phase = "idle"
        self.prepared: PreparedTurn | None = None
        self.outcome = TurnOutcome()
        self.lock = asyncio.Lock()
        #: Set by /init: rebuild after its turn so a new AGENTS.md is loaded.
        self.reload_after_turn = False

    @property
    def thread_id(self) -> str:
        """The session id (also the undo journal's ``session_id``)."""
        return self.state.thread_id

    @property
    def workdir(self) -> str:
        """The session's project folder (absolute)."""
        return self.state.workdir

    @property
    def agent(self):
        """The session's agent, built on first use."""
        if self._agent is None:
            self._agent = self._build_agent(self)
        return self._agent

    def config(self) -> LunaConfig:
        """Project config with this session's provider/model applied on top."""
        workdir = self.workdir
        try:
            cfg = load_config({"workdir": workdir}, cwd=workdir)
        except (LunaConfigError, OSError):
            cfg = LunaConfig(workdir=workdir)
        if self.state.provider is not None:
            cfg.provider = self.state.provider
            cfg.model = self.state.model
        elif self.state.model is not None:
            cfg.model = self.state.model
        return cfg

    def rebuild(self) -> None:
        """Rebuild this session's agent (raises on a bad config)."""
        self._agent = self._build_agent(self)

    def _switch(self, provider: str | None, model: str | None) -> None:
        previous = (self.state.provider, self.state.model)
        self.state.provider, self.state.model = provider, model
        try:
            self.rebuild()
        except Exception:
            self.state.provider, self.state.model = previous
            raise
        self.save()

    def switch_model(self, model: str) -> None:
        """Use ``model`` for this session only; restores the old one on failure."""
        self._switch(self.state.provider, model)

    def switch_provider(self, provider: str) -> None:
        """Use ``provider`` (with its default model) for this session only."""
        self._switch(provider, None)

    def set_plan(self, on: bool) -> None:
        """Toggle plan mode for this session (the agent reads it per tool call)."""
        self.state.plan = on
        self.save()

    def save(self) -> None:
        """Persist the session state."""
        self._store.save(self.state)

    def finish(self) -> None:
        """Return to idle after a turn (successful or not) and persist state."""
        self.phase = "idle"
        self.prepared = None
        self.outcome = TurnOutcome()
        self.save()


class RuntimeRegistry:
    """LRU of session runtimes; evicted ones are rebuilt from the state table."""

    def __init__(self, build_agent: Callable[[SessionRuntime], object], *, capacity: int = 8):
        self._build_agent = build_agent
        self._capacity = capacity
        self._items: OrderedDict[str, SessionRuntime] = OrderedDict()
        self._store: SessionStateStore | None = None

    def get(self, thread_id: str, workdir: str) -> SessionRuntime:
        """Return the runtime for ``thread_id`` (created or reloaded from the state table)."""
        resolved = str(Path(workdir).resolve())
        runtime = self._items.get(thread_id)
        if runtime is not None:
            if runtime.workdir != resolved:
                raise WorkdirMismatch(thread_id)
            self._items.move_to_end(thread_id)
            return runtime
        if self._store is None:
            self._store = SessionStateStore()
        state = self._store.load(thread_id, resolved)
        if state.workdir != resolved:
            raise WorkdirMismatch(thread_id)
        runtime = SessionRuntime(state, store=self._store, build_agent=self._build_agent)
        self._items[thread_id] = runtime
        for key in list(self._items):
            if len(self._items) <= self._capacity:
                break
            candidate = self._items[key]
            # never evict a streaming turn or one paused on an approval: its
            # PreparedTurn (dirty_before, phase) lives only in memory
            if not candidate.lock.locked() and candidate.phase == "idle" and key != thread_id:
                del self._items[key]
        return runtime


def runtime_for(request: Request, thread_id: str, workdir: str) -> SessionRuntime | JSONResponse:
    """Return the request's session runtime, or a 400 when the folder does not match."""
    try:
        return request.app.state.runtimes.get(thread_id, workdir)
    except WorkdirMismatch:
        return JSONResponse({"error": "workdir_mismatch"}, status_code=400)
