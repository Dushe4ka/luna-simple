"""The assembly journal: one row per subagent run, auto-profile versions, notices.

Lives in the same SQLite file as the session event log (``sessions.db``) but
uses its own connection, so the runner can write to it without touching the
event log's lock. Only what the *parent* wrote is stored: the assembly
(prompt, tools, skills, model, context) and the head of the task. Tool
output and the subagent's answer never enter the journal.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: How much of the task text the journal keeps.
TASK_HEAD = 200

_DDL = """
CREATE TABLE IF NOT EXISTS subagent_runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    workdir TEXT NOT NULL,
    parent_session TEXT NOT NULL,
    child_session TEXT NOT NULL,
    base_profile TEXT NOT NULL,
    dynamic INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    task_head TEXT NOT NULL,
    ok INTEGER NOT NULL,
    cancelled INTEGER NOT NULL,
    hit_max_steps INTEGER NOT NULL,
    steps INTEGER NOT NULL,
    cost REAL NOT NULL,
    mutating_json TEXT NOT NULL DEFAULT '[]',
    profile_name TEXT
);
CREATE INDEX IF NOT EXISTS subagent_runs_pending ON subagent_runs(profile_name, id);
CREATE INDEX IF NOT EXISTS subagent_runs_by_profile ON subagent_runs(base_profile, id);
CREATE TABLE IF NOT EXISTS subagent_profile_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    version INTEGER NOT NULL,
    spec_json TEXT,
    prev_json TEXT,
    note TEXT NOT NULL DEFAULT '',
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS luna_notices(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subagent_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_RUN_COLS = (
    "id, ts, workdir, parent_session, child_session, base_profile, dynamic, spec_json, "
    "task_head, ok, cancelled, hit_max_steps, steps, cost, mutating_json, profile_name"
)
_VER_COLS = "id, ts, name, path, action, version, spec_json, prev_json, note, undone"


@dataclass(frozen=True)
class RunRecord:
    """One subagent run as the journal keeps it."""

    id: int
    ts: float
    workdir: str
    parent_session: str
    child_session: str
    base_profile: str
    dynamic: bool
    spec: dict
    task_head: str
    ok: bool
    cancelled: bool
    hit_max_steps: bool
    steps: int
    cost: float
    mutating: tuple[str, ...] = ()
    profile_name: str | None = None

    @property
    def success(self) -> bool:
        """Finished without error, cancel or running out of steps (a rough measure)."""
        return self.ok and not self.cancelled and not self.hit_max_steps


@dataclass(frozen=True)
class ProfileVersion:
    """One saved change of a profile (for history and undo)."""

    id: int
    ts: float
    name: str
    path: str
    action: str
    version: int
    spec: dict | None
    prev: dict | None
    note: str
    undone: bool


def _loads(text: str | None) -> dict | None:
    return None if text is None else json.loads(text)


def _run(row: tuple) -> RunRecord:
    return RunRecord(
        id=row[0],
        ts=row[1],
        workdir=row[2],
        parent_session=row[3],
        child_session=row[4],
        base_profile=row[5],
        dynamic=bool(row[6]),
        spec=json.loads(row[7]),
        task_head=row[8],
        ok=bool(row[9]),
        cancelled=bool(row[10]),
        hit_max_steps=bool(row[11]),
        steps=row[12],
        cost=row[13],
        mutating=tuple(json.loads(row[14])),
        profile_name=row[15],
    )


def _version(row: tuple) -> ProfileVersion:
    return ProfileVersion(
        id=row[0],
        ts=row[1],
        name=row[2],
        path=row[3],
        action=row[4],
        version=row[5],
        spec=_loads(row[6]),
        prev=_loads(row[7]),
        note=row[8],
        undone=bool(row[9]),
    )


class AssemblyLog:
    """The assembly journal over ``path`` (usually ``sessions.db``)."""

    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_DDL)
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def _write(self):
        """Process lock plus one ``BEGIN IMMEDIATE`` transaction (atomic across processes)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _read(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # --- runs -------------------------------------------------------------

    def record(
        self,
        *,
        workdir: str,
        parent_session: str,
        child_session: str,
        base_profile: str,
        dynamic: bool,
        spec: dict,
        task: str,
        ok: bool,
        cancelled: bool,
        hit_max_steps: bool,
        steps: int,
        cost: float,
        mutating: Iterable[str] = (),
        now: float | None = None,
    ) -> RunRecord:
        """Append one run and return it."""
        ts = time.time() if now is None else now
        head = task[:TASK_HEAD]
        muts = list(mutating)
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO subagent_runs(ts, workdir, parent_session, child_session, "
                "base_profile, dynamic, spec_json, task_head, ok, cancelled, hit_max_steps, "
                "steps, cost, mutating_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts,
                    workdir,
                    parent_session,
                    child_session,
                    base_profile,
                    int(dynamic),
                    json.dumps(spec, ensure_ascii=False),
                    head,
                    int(ok),
                    int(cancelled),
                    int(hit_max_steps),
                    steps,
                    cost,
                    json.dumps(muts),
                ),
            )
            run_id = cur.lastrowid
        return RunRecord(
            run_id,
            ts,
            workdir,
            parent_session,
            child_session,
            base_profile,
            dynamic,
            spec,
            head,
            ok,
            cancelled,
            hit_max_steps,
            steps,
            cost,
            tuple(muts),
        )

    def get(self, run_id: int) -> RunRecord | None:
        """Return the run ``run_id``, or None."""
        rows = self._read(f"SELECT {_RUN_COLS} FROM subagent_runs WHERE id = ?", (run_id,))
        return _run(rows[0]) if rows else None

    def pending(self, limit: int = 200) -> list[RunRecord]:
        """Return the newest ``limit`` runs not yet folded into a profile, oldest first."""
        rows = self._read(
            f"SELECT {_RUN_COLS} FROM subagent_runs WHERE profile_name IS NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [_run(r) for r in reversed(rows)]

    def last_id(self) -> int:
        """Return the newest run id (0 when the journal is empty)."""
        return self._read("SELECT COALESCE(MAX(id), 0) FROM subagent_runs")[0][0]

    def count_after(self, run_id: int) -> int:
        """How many runs were recorded after ``run_id``."""
        return self._read("SELECT COUNT(*) FROM subagent_runs WHERE id > ?", (run_id,))[0][0]

    def mark(self, ids: Iterable[int], profile_name: str) -> None:
        """Mark runs as folded into ``profile_name``."""
        with self._write() as conn:
            conn.executemany(
                "UPDATE subagent_runs SET profile_name = ? WHERE id = ?",
                [(profile_name, i) for i in ids],
            )

    def recent_for(self, profile_name: str, limit: int = 10) -> list[RunRecord]:
        """Return the newest runs that used profile ``profile_name`` as their base."""
        rows = self._read(
            f"SELECT {_RUN_COLS} FROM subagent_runs WHERE base_profile = ? "
            "ORDER BY id DESC LIMIT ?",
            (profile_name, limit),
        )
        return [_run(r) for r in rows]

    # --- profile versions -------------------------------------------------

    def add_version(
        self,
        *,
        name: str,
        path: str,
        action: str,
        version: int,
        spec: dict | None,
        prev: dict | None,
        note: str = "",
        now: float | None = None,
    ) -> ProfileVersion:
        """Record a change of profile ``name`` in ``path``."""
        ts = time.time() if now is None else now
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO subagent_profile_versions(ts, name, path, action, version, "
                "spec_json, prev_json, note) VALUES (?,?,?,?,?,?,?,?)",
                (
                    ts,
                    name,
                    path,
                    action,
                    version,
                    None if spec is None else json.dumps(spec, ensure_ascii=False),
                    None if prev is None else json.dumps(prev, ensure_ascii=False),
                    note,
                ),
            )
            vid = cur.lastrowid
        return ProfileVersion(vid, ts, name, path, action, version, spec, prev, note, False)

    def versions(self, name: str | None = None, limit: int = 50) -> list[ProfileVersion]:
        """Profile changes, newest first (all profiles when ``name`` is None)."""
        if name is None:
            rows = self._read(
                f"SELECT {_VER_COLS} FROM subagent_profile_versions ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self._read(
                f"SELECT {_VER_COLS} FROM subagent_profile_versions WHERE name = ? "
                "ORDER BY id DESC LIMIT ?",
                (name, limit),
            )
        return [_version(r) for r in rows]

    def set_undone(self, version_id: int) -> None:
        """Mark a change as rolled back."""
        with self._write() as conn:
            conn.execute(
                "UPDATE subagent_profile_versions SET undone = 1 WHERE id = ?", (version_id,)
            )

    # --- notices and meta -------------------------------------------------

    def push_notice(self, level: str, text: str) -> None:
        """Queue a notice for the next finished turn (any session of this user)."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO luna_notices(ts, level, text) VALUES (?,?,?)",
                (time.time(), level, text),
            )

    def drain_notices(self) -> list[tuple[str, str]]:
        """Take every queued notice (oldest first); each is returned once."""
        with self._write() as conn:
            rows = conn.execute("SELECT id, level, text FROM luna_notices ORDER BY id").fetchall()
            if rows:
                conn.execute("DELETE FROM luna_notices WHERE id <= ?", (rows[-1][0],))
        return [(level, text) for _id, level, text in rows]

    def get_meta(self, key: str) -> str | None:
        """Return the stored value, or None."""
        rows = self._read("SELECT value FROM subagent_meta WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        """Store ``value`` under ``key``."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO subagent_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def claim_meta(self, key: str, *, expect: str | None, value: str) -> bool:
        """Set ``key`` to ``value`` only if it currently equals ``expect`` (None = absent)."""
        with self._write() as conn:
            row = conn.execute("SELECT value FROM subagent_meta WHERE key = ?", (key,)).fetchone()
            if (row[0] if row else None) != expect:
                return False
            conn.execute(
                "INSERT INTO subagent_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            return True

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()


__all__ = ["TASK_HEAD", "AssemblyLog", "ProfileVersion", "RunRecord"]
