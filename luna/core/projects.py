"""Persisted projects: exact launch directories and whether they are trusted.

Lives in the same ``sessions.db`` as :class:`luna.core.persistence.SessionIndex`
so a future desktop client can list projects and their sessions from one
store. Best-effort like the session index: an unavailable database degrades
to a no-op index instead of breaking the CLI.
"""

from __future__ import annotations

import sqlite3
import time
from collections import namedtuple
from collections.abc import Mapping
from pathlib import Path

from luna.core.persistence import _INDEX_ERRORS, _db_path

ProjectRow = namedtuple("ProjectRow", "path trusted_at last_opened")


def _key(path: str) -> str:
    return str(Path(path).resolve())


def _migrate(conn: sqlite3.Connection) -> None:
    """Trust every still-existing folder that already has sessions.

    Runs only when ``luna_projects`` is first created, so existing users are
    never re-prompted for folders they already worked in, while folders that
    gain sessions later (e.g. via a scripted ``-p`` run) are not silently
    trusted.
    """
    try:
        rows = conn.execute(
            "SELECT workdir, MIN(created) FROM luna_sessions GROUP BY workdir"
        ).fetchall()
    except sqlite3.OperationalError:
        return  # no luna_sessions table yet: nothing to migrate
    for workdir, created in rows:
        if Path(workdir).is_dir():
            conn.execute(
                "INSERT OR IGNORE INTO luna_projects VALUES (?, ?, ?)",
                (workdir, created, created),
            )


class ProjectIndex:
    """The ``luna_projects`` table inside ``sessions.db`` (best-effort)."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        """Open ``sessions.db``, create the table, migrate once; degrade on failure."""
        self._conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(_db_path(env), check_same_thread=False)
            existed = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'luna_projects'"
            ).fetchone()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS luna_projects ("
                "path TEXT PRIMARY KEY, trusted_at REAL, last_opened REAL NOT NULL)"
            )
            if not existed:
                _migrate(conn)
            conn.commit()
            self._conn = conn
        except _INDEX_ERRORS:
            self._conn = None

    @property
    def ok(self) -> bool:
        """True when the backing table is available."""
        return self._conn is not None

    def is_trusted(self, path: str) -> bool:
        """Whether ``path`` has been trusted; ``False`` if unavailable."""
        if self._conn is None:
            return False
        try:
            row = self._conn.execute(
                "SELECT trusted_at FROM luna_projects WHERE path = ?", (_key(path),)
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None and row[0] is not None

    def trust(self, path: str) -> None:
        """Mark ``path`` trusted and opened now (no-op if unavailable)."""
        if self._conn is None:
            return
        now = time.time()
        try:
            self._conn.execute(
                "INSERT INTO luna_projects VALUES (?, ?, ?) ON CONFLICT(path) DO UPDATE "
                "SET trusted_at = excluded.trusted_at, last_opened = excluded.last_opened",
                (_key(path), now, now),
            )
            self._conn.commit()
        except sqlite3.Error:
            pass

    def touch(self, path: str) -> None:
        """Bump ``last_opened`` (no-op if unavailable)."""
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "UPDATE luna_projects SET last_opened = ? WHERE path = ?",
                (time.time(), _key(path)),
            )
            self._conn.commit()
        except sqlite3.Error:
            pass

    def list(self) -> list[ProjectRow]:
        """Projects, most recently opened first; ``[]`` if unavailable."""
        if self._conn is None:
            return []
        try:
            return [
                ProjectRow(*r)
                for r in self._conn.execute(
                    "SELECT path, trusted_at, last_opened FROM luna_projects "
                    "ORDER BY last_opened DESC"
                )
            ]
        except sqlite3.Error:
            return []
