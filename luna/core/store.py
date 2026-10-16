"""The app's session store: the core event log in ``<config_dir>/sessions.db``."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from luna_core.session import EventLog

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
