"""Durable SQLite checkpointer and a small session index for Luna.

This is the only module besides ``agent``/``session`` that touches langgraph.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import namedtuple
from collections.abc import Mapping
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from luna.config import config_dir

Row = namedtuple("Row", "thread_id workdir created updated title")

_TITLE_MAX = 72
_WS = re.compile(r"\s+")


def _db_path(env: Mapping[str, str] | None) -> Path:
    path = config_dir(env) / "sessions.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def make_title(text: str) -> str:
    """Collapse whitespace and truncate for the session list."""
    clean = _WS.sub(" ", text).strip()
    return clean if len(clean) <= _TITLE_MAX else clean[:_TITLE_MAX] + "…"


def checkpointer(env: Mapping[str, str] | None = None) -> SqliteSaver:
    """Return a :class:`SqliteSaver` bound to ``<config_dir>/sessions.db``."""
    conn = sqlite3.connect(_db_path(env), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


class SessionIndex:
    """The ``luna_sessions`` table living inside ``sessions.db``."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        """Open ``sessions.db`` and ensure the ``luna_sessions`` table exists."""
        self._conn = sqlite3.connect(_db_path(env), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS luna_sessions ("
            "thread_id TEXT PRIMARY KEY, workdir TEXT NOT NULL, "
            "created REAL NOT NULL, updated REAL NOT NULL, title TEXT NOT NULL)"
        )
        self._conn.commit()

    def record(self, thread_id: str, workdir: str, title: str) -> None:
        """Insert a new session row, keeping any existing row untouched."""
        now = time.time()
        self._conn.execute(
            "INSERT OR IGNORE INTO luna_sessions VALUES (?, ?, ?, ?, ?)",
            (thread_id, str(Path(workdir).resolve()), now, now, title),
        )
        self._conn.commit()

    def touch(self, thread_id: str) -> None:
        """Bump the ``updated`` timestamp of an existing session."""
        self._conn.execute(
            "UPDATE luna_sessions SET updated = ? WHERE thread_id = ?",
            (time.time(), thread_id),
        )
        self._conn.commit()

    def latest_for(self, workdir: str) -> Row | None:
        """Return the most recently updated session for ``workdir``, if any."""
        cur = self._conn.execute(
            "SELECT thread_id, workdir, created, updated, title FROM luna_sessions "
            "WHERE workdir = ? ORDER BY updated DESC LIMIT 1",
            (str(Path(workdir).resolve()),),
        )
        row = cur.fetchone()
        return Row(*row) if row else None

    def list(self, workdir: str | None = None, limit: int = 20) -> list[Row]:
        """Return sessions newest ``updated`` first, optionally filtered by workdir."""
        sql = "SELECT thread_id, workdir, created, updated, title FROM luna_sessions"
        params: tuple = ()
        if workdir is not None:
            sql += " WHERE workdir = ?"
            params = (str(Path(workdir).resolve()),)
        sql += " ORDER BY updated DESC LIMIT ?"
        return [Row(*r) for r in self._conn.execute(sql, (*params, limit))]
