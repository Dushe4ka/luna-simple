"""The app's session store: the core event log in ``<config_dir>/sessions.db``."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from luna_core.session import EventLog, SessionInfo

from luna.config.config import config_dir

#: What opening ``sessions.db`` may raise; callers degrade instead of crashing.
INDEX_ERRORS = (sqlite3.Error, OSError)

_TITLE_MAX = 72
_WS = re.compile(r"\s+")
_LEGACY = (
    "checkpoints",
    "writes",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
    "luna_sessions",
)
_LOGS: dict[str, EventLog] = {}


def db_path(env: Mapping[str, str] | None = None) -> Path:
    """Return ``<config_dir>/sessions.db`` (its directory is created)."""
    path = config_dir(env) / "sessions.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _drop_legacy(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        with conn:
            for table in _LEGACY:
                conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    finally:
        conn.close()


def open_log(env: Mapping[str, str] | None = None) -> EventLog:
    """Return the process-wide event log (legacy LangGraph tables are dropped on first open)."""
    path = db_path(env)
    key = str(path)
    if key not in _LOGS:
        _drop_legacy(path)
        _LOGS[key] = EventLog(path)
    return _LOGS[key]


def make_title(text: str) -> str:
    """Collapse whitespace and truncate for the session list."""
    clean = _WS.sub(" ", text).strip()
    return clean if len(clean) <= _TITLE_MAX else clean[:_TITLE_MAX] + "…"


def is_conversation(info) -> bool:
    """Top-level session or a fork (a fork is a conversation of its own); not a child run."""
    return info.parent_id is None or info.meta.get("kind") == "fork"


def conversations(log: EventLog, workdir: str, limit: int = 100) -> list[SessionInfo]:
    """Top-level sessions of ``workdir`` that hold a conversation, newest first.

    Subagent/background children and sessions nobody typed into (an
    empty REPL start or ``/new``) are skipped, so ``-c`` and the pickers
    land on real work.
    """
    out = []
    for info in log.list(workdir, limit=limit):
        if is_conversation(info) and any(r.type == "user" for r in log.records(info.id)):
            out.append(info)
    return out
