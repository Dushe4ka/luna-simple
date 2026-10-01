"""Per-session settings persisted in ``sessions.db``.

Model, provider, plan mode, pinned files, carried-over diagnostics and token
usage belong to one conversation, not to a project: two open sessions in
the same folder must not see each other's plan mode or model. Best-effort
like :class:`luna.core.persistence.SessionIndex`: an unavailable database
yields in-memory defaults instead of breaking the caller.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from luna.config.usage import SessionUsage, TurnUsage
from luna.core.persistence import _INDEX_ERRORS, _db_path
from luna.turn.context import PinnedFiles


@dataclass
class SessionState:
    """Everything that is scoped to one conversation thread."""

    thread_id: str
    workdir: str
    provider: str | None = None
    model: str | None = None
    plan: bool = False
    pinned: PinnedFiles = field(default_factory=PinnedFiles)
    pending_diagnostics: str = ""
    usage: SessionUsage = field(default_factory=SessionUsage)


def _usage_to_json(usage: SessionUsage) -> str:
    return json.dumps([[t.input_tokens, t.output_tokens, t.total_tokens] for t in usage.turns])


def _usage_from_json(text: str) -> SessionUsage:
    try:
        rows = json.loads(text or "[]")
    except ValueError:
        rows = []
    return SessionUsage(turns=[TurnUsage(*row) for row in rows if len(row) == 3])


class SessionStateStore:
    """The ``luna_session_state`` table inside ``sessions.db`` (best-effort)."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        """Open ``sessions.db`` and create the table; degrade to a no-op on failure."""
        self._conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(_db_path(env), check_same_thread=False)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS luna_session_state ("
                "thread_id TEXT PRIMARY KEY, workdir TEXT NOT NULL, provider TEXT, "
                "model TEXT, plan INTEGER NOT NULL DEFAULT 0, "
                "pinned TEXT NOT NULL DEFAULT '[]', "
                "pending_diagnostics TEXT NOT NULL DEFAULT '', "
                "usage TEXT NOT NULL DEFAULT '[]')"
            )
            conn.commit()
            self._conn = conn
        except _INDEX_ERRORS:
            self._conn = None

    @property
    def ok(self) -> bool:
        """True when the backing table is available."""
        return self._conn is not None

    def load(self, thread_id: str, workdir: str) -> SessionState:
        """Return the stored state for ``thread_id``, or defaults when unknown/unavailable."""
        state = SessionState(thread_id, workdir)
        if self._conn is None:
            return state
        try:
            row = self._conn.execute(
                "SELECT workdir, provider, model, plan, pinned, pending_diagnostics, usage "
                "FROM luna_session_state WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        except sqlite3.Error:
            return state
        if row is None:
            return state
        stored_workdir, provider, model, plan, pinned, diagnostics, usage = row
        # the stored folder wins: a thread id belongs to exactly one project
        state.workdir = stored_workdir
        state.provider, state.model, state.plan = provider, model, bool(plan)
        try:
            state.pinned.add(*json.loads(pinned or "[]"))
        except ValueError:
            pass
        state.pending_diagnostics = diagnostics or ""
        state.usage = _usage_from_json(usage)
        return state

    def save(self, state: SessionState) -> None:
        """Upsert ``state`` (no-op if unavailable)."""
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO luna_session_state VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET workdir = excluded.workdir, "
                "provider = excluded.provider, model = excluded.model, "
                "plan = excluded.plan, pinned = excluded.pinned, "
                "pending_diagnostics = excluded.pending_diagnostics, usage = excluded.usage",
                (
                    state.thread_id,
                    state.workdir,
                    state.provider,
                    state.model,
                    int(state.plan),
                    json.dumps(state.pinned.paths),
                    state.pending_diagnostics,
                    _usage_to_json(state.usage),
                ),
            )
            self._conn.commit()
        except sqlite3.Error:
            pass
