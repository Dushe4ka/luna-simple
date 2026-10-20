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


def _drop_legacy(path: Path, env: Mapping[str, str] | None = None) -> None:
    # Trusted folders are derived from the legacy ``luna_sessions`` table the first time
    # ``luna_projects`` is created; do that before the table goes away.
    from luna.core.projects import ProjectIndex

    ProjectIndex(env)
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
        _drop_legacy(path, env)
        _LOGS[key] = EventLog(path)
    return _LOGS[key]


def make_title(text: str) -> str:
    """Collapse whitespace and truncate for the session list."""
    clean = _WS.sub(" ", text).strip()
    return clean if len(clean) <= _TITLE_MAX else clean[:_TITLE_MAX] + "…"


#: ``EventLog.list`` limit meaning "every session" (rows are small; filtering comes after).
_ALL = 1_000_000_000


def is_conversation(info) -> bool:
    """Top-level session or a fork (a fork is a conversation of its own); not a child run."""
    return info.parent_id is None or info.meta.get("kind") == "fork"


def conversations(log: EventLog, workdir: str | None, limit: int = 100) -> list[SessionInfo]:
    """Top-level sessions of ``workdir`` (every folder when ``None``) that hold a conversation.

    Newest first.

    Subagent/background children and sessions nobody typed into (an
    empty REPL start or ``/new``) are skipped, so ``-c`` and the pickers
    land on real work. Filtering happens before ``limit`` is applied: many
    newer child runs must not push real conversations out of the list.
    """
    out = []
    for info in log.list(workdir, limit=_ALL):
        if is_conversation(info) and any(r.type == "user" for r in log.records(info.id)):
            out.append(info)
            if len(out) >= limit:
                break
    return out


def descendants(log: EventLog, session_id: str) -> list[str]:
    """Ids of the subagent/background runs below ``session_id``, children before parents.

    Forks keep the parent's id but are conversations of their own, so they
    (and what hangs below them) are not part of the result.
    """
    out: list[str] = []
    for child in log.children(session_id):
        if child.meta.get("kind") == "fork":
            continue
        out.extend(descendants(log, child.id))
        out.append(child.id)
    return out
