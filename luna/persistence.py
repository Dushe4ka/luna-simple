"""Durable SQLite checkpointer and a small session index for Luna.

This is the only module besides ``agent``/``session`` that touches langgraph.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import namedtuple
from collections.abc import Callable, Mapping
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from luna.config import config_dir

Row = namedtuple("Row", "thread_id workdir created updated title")

_TITLE_MAX = 72
_WS = re.compile(r"\s+")
_INDEX_ERRORS = (sqlite3.Error, OSError)


def _db_path(env: Mapping[str, str] | None) -> Path:
    path = config_dir(env) / "sessions.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def make_title(text: str) -> str:
    """Collapse whitespace and truncate for the session list."""
    clean = _WS.sub(" ", text).strip()
    return clean if len(clean) <= _TITLE_MAX else clean[:_TITLE_MAX] + "…"


def checkpointer(
    env: Mapping[str, str] | None = None,
    *,
    on_warn: Callable[[str], None] | None = None,
):
    """Return a :class:`SqliteSaver` on ``sessions.db``, else an in-memory saver."""
    try:
        conn = sqlite3.connect(_db_path(env), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        return saver
    except _INDEX_ERRORS as exc:
        if on_warn is not None:
            on_warn(f"sessions.db unavailable ({exc}); this session will not be saved")
        return InMemorySaver()


class SessionIndex:
    """The ``luna_sessions`` table living inside ``sessions.db`` (best-effort)."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        """Open ``sessions.db``; degrade to a no-op index if that fails."""
        self._conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(_db_path(env), check_same_thread=False)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS luna_sessions ("
                "thread_id TEXT PRIMARY KEY, workdir TEXT NOT NULL, "
                "created REAL NOT NULL, updated REAL NOT NULL, title TEXT NOT NULL)"
            )
            conn.commit()
            self._conn = conn
        except _INDEX_ERRORS:
            self._conn = None

    @property
    def ok(self) -> bool:
        """True when the backing table is available."""
        return self._conn is not None

    def record(self, thread_id: str, workdir: str, title: str) -> None:
        """Insert a new session row (no-op if the index is unavailable)."""
        if self._conn is None:
            return
        now = time.time()
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO luna_sessions VALUES (?, ?, ?, ?, ?)",
                (thread_id, str(Path(workdir).resolve()), now, now, title),
            )
            self._conn.commit()
        except sqlite3.Error:
            pass

    def touch(self, thread_id: str) -> None:
        """Bump ``updated`` (no-op if unavailable)."""
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "UPDATE luna_sessions SET updated = ? WHERE thread_id = ?",
                (time.time(), thread_id),
            )
            self._conn.commit()
        except sqlite3.Error:
            pass

    def latest_for(self, workdir: str) -> Row | None:
        """Most recently updated session for ``workdir``; ``None`` if unavailable."""
        if self._conn is None:
            return None
        try:
            cur = self._conn.execute(
                "SELECT thread_id, workdir, created, updated, title FROM luna_sessions "
                "WHERE workdir = ? ORDER BY updated DESC LIMIT 1",
                (str(Path(workdir).resolve()),),
            )
            row = cur.fetchone()
        except sqlite3.Error:
            return None
        return Row(*row) if row else None

    def list(self, workdir: str | None = None, limit: int = 20) -> list[Row]:
        """Sessions newest first; ``[]`` if unavailable."""
        if self._conn is None:
            return []
        sql = "SELECT thread_id, workdir, created, updated, title FROM luna_sessions"
        params: tuple = ()
        if workdir is not None:
            sql += " WHERE workdir = ?"
            params = (str(Path(workdir).resolve()),)
        sql += " ORDER BY updated DESC LIMIT ?"
        try:
            return [Row(*r) for r in self._conn.execute(sql, (*params, limit))]
        except sqlite3.Error:
            return []
