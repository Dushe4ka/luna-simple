"""Sessions stored as an append-only event log in SQLite."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from luna_core.errors import LunaCoreError
from luna_core.messages import Message, Text, ToolResult, message_from_dict
from luna_core.policy import Decision
from luna_core.undo import UndoJournal, UndoResult
from luna_core.usage import Usage

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_MESSAGE_TYPES = ("user", "assistant", "tool_results")
_DDL = """
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    workdir TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    parent_id TEXT,
    fork_seq INTEGER,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events(
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    v INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS sessions_by_workdir ON sessions(workdir, updated);
"""


@dataclass(frozen=True)
class Record:
    """One stored event."""

    seq: int
    ts: float
    type: str
    payload: dict


@dataclass(frozen=True)
class SessionInfo:
    """A session row."""

    id: str
    workdir: str
    title: str
    created: float
    updated: float
    parent_id: str | None
    fork_seq: int | None
    meta: dict


@dataclass(frozen=True)
class Entry:
    """A message with its position and turn."""

    seq: int
    turn_id: str | None
    message: Message


class EventLog:
    """The SQLite store shared by all sessions of a process."""

    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_DDL)
        self._lock = threading.Lock()

    def create(
        self,
        workdir: str,
        *,
        title: str = "",
        parent_id: str | None = None,
        fork_seq: int | None = None,
        session_id: str | None = None,
        meta: dict | None = None,
    ) -> str:
        """Create a session and return its id."""
        sid = session_id or uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(id, workdir, title, created, updated, parent_id, "
                "fork_seq, meta) VALUES (?,?,?,?,?,?,?,?)",
                (sid, workdir, title, now, now, parent_id, fork_seq, json.dumps(meta or {})),
            )
        return sid

    @contextlib.contextmanager
    def _write(self):
        """Hold the process lock and one ``BEGIN IMMEDIATE`` transaction.

        ``IMMEDIATE`` takes SQLite's write lock up front, so a
        read-modify-write inside is atomic across threads *and* across
        other connections (another ``EventLog`` or process) on the file.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _require(conn: sqlite3.Connection, session_id: str) -> None:
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
            raise LunaCoreError(f"no session {session_id!r} (it was deleted or never existed)")

    @staticmethod
    def _insert(
        conn: sqlite3.Connection, session_id: str, type_: str, payload: dict, now: float
    ) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()
        seq = int(row[0]) + 1
        conn.execute(
            "INSERT INTO events(session_id, seq, ts, type, payload, v) VALUES (?,?,?,?,?,?)",
            (
                session_id,
                seq,
                now,
                type_,
                json.dumps(payload, ensure_ascii=False),
                SCHEMA_VERSION,
            ),
        )
        return seq

    def append(self, session_id: str, type_: str, payload: dict) -> int:
        """Append one event; returns its sequence number.

        Raises :class:`LunaCoreError` when the session does not exist
        (deleted or never created); nothing is written then.
        """
        now = time.time()
        with self._write() as conn:
            self._require(conn, session_id)
            seq = self._insert(conn, session_id, type_, payload, now)
            conn.execute("UPDATE sessions SET updated = ? WHERE id = ?", (now, session_id))
        return seq

    def _fork(self, session_id: str, at_seq: int | None, *, workdir: str, title: str) -> str:
        """Copy ``session_id``'s events up to ``at_seq`` into a new fork, atomically."""
        child_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self._write() as conn:
            self._require(conn, session_id)
            rows = conn.execute(
                "SELECT type, payload FROM events "
                "WHERE session_id = ? AND (? IS NULL OR seq <= ?) ORDER BY seq",
                (session_id, at_seq, at_seq),
            ).fetchall()
            conn.execute(
                "INSERT INTO sessions(id, workdir, title, created, updated, parent_id, "
                "fork_seq, meta) VALUES (?,?,?,?,?,?,?,?)",
                (
                    child_id,
                    workdir,
                    title,
                    now,
                    now,
                    session_id,
                    at_seq,
                    json.dumps({"kind": "fork"}),
                ),
            )
            for type_, payload in rows:
                self._insert(conn, child_id, type_, json.loads(payload), now)
        return child_id

    def records(self, session_id: str, after: int = 0) -> list[Record]:
        """Events of a session after ``after``, in order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, type, payload FROM events "
                "WHERE session_id = ? AND seq > ? ORDER BY seq",
                (session_id, after),
            ).fetchall()
        return [Record(seq, ts, t, json.loads(p)) for seq, ts, t, p in rows]

    def _info(self, row: tuple) -> SessionInfo:
        sid, workdir, title, created, updated, parent, fork_seq, meta = row
        return SessionInfo(
            sid, workdir, title, created, updated, parent, fork_seq, json.loads(meta or "{}")
        )

    def info(self, session_id: str) -> SessionInfo | None:
        """Return the session row, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, workdir, title, created, updated, parent_id, fork_seq, meta "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return self._info(row) if row else None

    def list(self, workdir: str | None = None, limit: int = 100) -> list[SessionInfo]:
        """Sessions, most recently updated first."""
        sql = "SELECT id, workdir, title, created, updated, parent_id, fork_seq, meta FROM sessions"
        args: tuple = ()
        if workdir is not None:
            sql += " WHERE workdir = ?"
            args = (workdir,)
        sql += " ORDER BY updated DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*args, limit)).fetchall()
        return [self._info(r) for r in rows]

    def children(self, session_id: str) -> list[SessionInfo]:
        """Sessions whose parent is ``session_id`` (forks, subagents, background tasks)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, workdir, title, created, updated, parent_id, fork_seq, meta "
                "FROM sessions WHERE parent_id = ? ORDER BY created",
                (session_id,),
            ).fetchall()
        return [self._info(r) for r in rows]

    def set_title(self, session_id: str, title: str) -> None:
        """Rename a session."""
        with self._lock:
            self._conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    def set_meta(self, session_id: str, **values: object) -> None:
        """Merge ``values`` into the session's metadata (atomic read-modify-write)."""
        with self._write() as conn:
            row = conn.execute("SELECT meta FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return
            meta = {**json.loads(row[0] or "{}"), **values}
            conn.execute(
                "UPDATE sessions SET meta = ? WHERE id = ?", (json.dumps(meta), session_id)
            )

    def delete(self, session_id: str) -> bool:
        """Delete a session, its events and its subagent/background runs, atomically.

        Children are deleted recursively, except forks (``meta.kind ==
        "fork"``): a fork is a conversation of its own, so it is kept and
        only loses its link to the deleted parent (``parent_id`` becomes
        ``None``; ``fork_seq`` and ``meta`` stay). Returns whether
        ``session_id`` existed.
        """
        with self._write() as conn:
            if (
                conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
                is None
            ):
                return False
            doomed = [session_id]
            i = 0
            while i < len(doomed):
                for child, meta in conn.execute(
                    "SELECT id, meta FROM sessions WHERE parent_id = ?", (doomed[i],)
                ).fetchall():
                    if json.loads(meta or "{}").get("kind") == "fork":
                        conn.execute("UPDATE sessions SET parent_id = NULL WHERE id = ?", (child,))
                    elif child not in doomed:
                        doomed.append(child)
                i += 1
            for sid in reversed(doomed):
                conn.execute("DELETE FROM events WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        return True

    def prune(self, older_than_days: float, workdir: str | None = None) -> int:
        """Delete sessions not updated for ``older_than_days``; returns how many."""
        cutoff = time.time() - older_than_days * 86400
        old = [i.id for i in self.list(workdir, limit=1_000_000) if i.updated < cutoff]
        for sid in old:
            self.delete(sid)
        return len(old)

    def close(self) -> None:
        """Close the database."""
        with self._lock:
            self._conn.close()


def patch_dangling(messages: list[Message]) -> list[Message]:
    """Give every tool call a result (synthetic "cancelled" ones where missing)."""
    out: list[Message] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        out.append(message)
        i += 1
        calls = message.tool_calls if message.role == "assistant" else []
        if not calls:
            continue
        follow = messages[i] if i < len(messages) and messages[i].role == "tool" else None
        have = {r.call_id for r in follow.results} if follow else set()
        missing = [
            ToolResult(c.id, (Text("cancelled: the call did not finish"),), True, c.name)
            for c in calls
            if c.id not in have
        ]
        if follow is not None:
            i += 1
            out.append(Message.tool_results([*follow.results, *missing]) if missing else follow)
        elif missing:
            out.append(Message.tool_results(missing))
    return out


class Session:
    """A conversation backed by the event log."""

    def __init__(self, log: EventLog, session_id: str, *, workdir: str, undo: bool = True) -> None:
        self.log = log
        self.id = session_id
        self.workdir = workdir
        self.state: dict = {}
        self.undo_journal = UndoJournal(workdir, session_id) if undo else None
        self._queue: list[str] = []
        self._decisions: dict[str, asyncio.Future] = {}
        self._cancel: asyncio.Event | None = None
        self._children: list[Session] = []
        self._listeners: list = []

    @classmethod
    def create(
        cls,
        log: EventLog,
        workdir: str,
        *,
        title: str = "",
        undo: bool = True,
        meta: dict | None = None,
    ) -> Session:
        """Create a new session in ``workdir``."""
        return cls(log, log.create(workdir, title=title, meta=meta), workdir=workdir, undo=undo)

    @classmethod
    def open(cls, log: EventLog, session_id: str, *, undo: bool = True) -> Session:
        """Open an existing session."""
        info = log.info(session_id)
        if info is None:
            raise LunaCoreError(f"no session {session_id!r}")
        return cls(log, session_id, workdir=info.workdir, undo=undo)

    # -- log ---------------------------------------------------------------
    def append(self, type_: str, payload: dict) -> int:
        """Append an event to this session."""
        seq = self.log.append(self.id, type_, payload)
        info = self.log.info(self.id) if type_ == "user" else None
        if info is not None and not info.title:
            text = message_from_dict(payload["message"]).text.strip().splitlines()
            if text:
                self.log.set_title(self.id, text[0][:80])
        return seq

    def records(self) -> list[Record]:
        """All events."""
        return self.log.records(self.id)

    @property
    def title(self) -> str:
        """Session title (first user line unless renamed)."""
        info = self.log.info(self.id)
        return info.title if info else ""

    def _scan(self) -> tuple[list[Entry], str | None, int, set[str], list[str]]:
        entries: list[Entry] = []
        summary: str | None = None
        cut = 0
        hidden: set[str] = set()
        redo: list[str] = []
        turn: str | None = None
        for rec in self.records():
            kind = rec.type
            if kind == "turn_started":
                turn = rec.payload["turn_id"]
                redo = []
            elif kind in _MESSAGE_TYPES:
                entries.append(Entry(rec.seq, turn, message_from_dict(rec.payload["message"])))
            elif kind == "compacted":
                summary, cut = rec.payload["summary"], int(rec.payload["upto_seq"])
            elif kind == "rewound":
                hidden.add(rec.payload["turn_id"])
                redo.append(rec.payload["turn_id"])
            elif kind == "restored":
                hidden.discard(rec.payload["turn_id"])
                if redo and redo[-1] == rec.payload["turn_id"]:
                    redo.pop()
        return entries, summary, cut, hidden, redo

    def entries(self) -> list[Entry]:
        """Visible messages (after compaction and rewinds), with positions."""
        entries, _summary, cut, hidden, _redo = self._scan()
        return [e for e in entries if e.seq > cut and e.turn_id not in hidden]

    def messages(self) -> list[Message]:
        """Build the model context: summary (if compacted) + visible messages, calls patched."""
        _entries, summary, _cut, _hidden, _redo = self._scan()
        out = (
            [Message.user(f"<summary of the earlier conversation>\n{summary}\n</summary>")]
            if summary
            else []
        )
        out += [e.message for e in self.entries()]
        return patch_dangling(out)

    @property
    def has_user_messages(self) -> bool:
        """True once any user message was logged."""
        return any(r.type == "user" for r in self.records())

    def turn_ids(self) -> list[str]:
        """Visible turns that have messages, oldest first."""
        seen: list[str] = []
        for e in self.entries():
            if e.turn_id and e.turn_id not in seen:
                seen.append(e.turn_id)
        return seen

    def usage(self) -> Usage:
        """Tokens used by every model request in this session."""
        total = Usage()
        for rec in self.records():
            if rec.type == "assistant" and rec.payload.get("usage"):
                total = total + Usage.from_dict(rec.payload["usage"])
        return total

    def cost(self) -> float:
        """USD spent by this session and its subagents (turns with a known price only)."""
        own = sum(
            float(r.payload.get("cost") or 0) for r in self.records() if r.type == "turn_done"
        )
        for info in self.log.children(self.id):
            if info.meta.get("kind") in ("subagent", "background"):
                own += Session(self.log, info.id, workdir=info.workdir, undo=False).cost()
        return own

    # -- children and listeners --------------------------------------------
    def adopt(self, child: Session) -> None:
        """Route approvals of ``child`` (a subagent session) through this session."""
        self._children.append(child)

    def release(self, child: Session) -> None:
        """Stop routing ``child``'s approvals."""
        if child in self._children:
            self._children.remove(child)

    def subscribe(self, fn) -> object:
        """Register ``fn`` for out-of-turn events (background completions); returns unsubscribe."""
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def notify(self, event: object) -> None:
        """Deliver an out-of-turn event to every listener (listener errors are ignored)."""
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception as exc:  # noqa: BLE001 - a broken listener must not break the agent
                logger.warning("session listener failed: %r", exc, exc_info=True)

    def todos(self) -> list[dict]:
        """Return the latest plan written with ``write_todos``."""
        for rec in reversed(self.records()):
            if rec.type == "todos":
                return rec.payload["todos"]
        return []

    # -- approvals ---------------------------------------------------------
    def expect_decision(self, call_id: str) -> asyncio.Future:
        """Return a future resolved by :meth:`decide` for ``call_id``."""
        fut = asyncio.get_running_loop().create_future()
        self._decisions[call_id] = fut
        return fut

    def decide(self, call_id: str, decision: Decision | dict) -> None:
        """Answer a pending approval."""
        fut = self._decisions.pop(call_id, None)
        if fut is None:
            for child in self._children:
                prefix = f"{child.id}:"
                if call_id.startswith(prefix):
                    child.decide(call_id[len(prefix) :], decision)
                    return
            raise LunaCoreError(f"no approval is pending for call {call_id!r}")
        if not fut.done():
            fut.set_result(
                decision if isinstance(decision, Decision) else Decision.from_dict(decision)
            )

    @property
    def pending_approvals(self) -> list[str]:
        """Call ids waiting for a decision.

        Ids of adopted subagent sessions carry their ``"<child id>:"`` prefix
        (recursively), exactly as their ``NeedsApproval`` events do, so two
        siblings' identical call ids never collide.
        """
        own = [cid for cid, fut in self._decisions.items() if not fut.done()]
        return own + [
            f"{child.id}:{cid}" for child in self._children for cid in child.pending_approvals
        ]

    # -- queue and turn control -------------------------------------------
    def queue(self, text: str) -> None:
        """Deliver ``text`` as a user message at the next step boundary."""
        self._queue.append(text)

    def drain_queue(self) -> list[str]:
        """Take all queued texts."""
        items, self._queue = self._queue, []
        return items

    @property
    def busy(self) -> bool:
        """True while a turn runs."""
        return self._cancel is not None

    def _begin_turn(self) -> asyncio.Event:
        if self._cancel is not None:
            raise LunaCoreError("a turn is already running in this session")
        self._cancel = asyncio.Event()
        return self._cancel

    def _end_turn(self) -> None:
        self._cancel = None
        for fut in self._decisions.values():
            if not fut.done():
                fut.cancel()
        self._decisions.clear()

    def cancel_turn(self) -> bool:
        """Ask the running turn to stop; False when idle."""
        if self._cancel is None:
            return False
        self._cancel.set()
        return True

    # -- history -----------------------------------------------------------
    @property
    def undo_depth(self) -> int:
        """How many turns /undo can revert."""
        return len(self.turn_ids())

    def undo(self) -> UndoResult | None:
        """Revert the last turn: files (when journaled) and conversation.

        The journal's newest record and the conversation's last visible
        turn can diverge — a turn that was cancelled after ``begin_turn``
        but before any message was logged leaves a journal record with no
        visible turn. Such ghost records are discarded (no files are
        touched for them) until the journal's top either matches the last
        visible turn or runs out; a turn with no journal record at all
        rewinds the conversation without touching any files.
        """
        turns = self.turn_ids()
        if not turns:
            return None
        last = turns[-1]
        note = "conversation rewound"
        if self.undo_journal is not None:
            top = self.undo_journal.top_turn()
            while top is not None and top not in turns:
                self.undo_journal.discard_top()
                top = self.undo_journal.top_turn()
            if top == last:
                res = self.undo_journal.undo()
                if res is not None:
                    note = f"{res.note}; conversation rewound"
            else:
                note = "files not journaled for this turn; conversation rewound"
        self.append("rewound", {"turn_id": last})
        return UndoResult(last, note)

    def redo(self) -> UndoResult | None:
        """Re-apply the most recently undone turn."""
        *_rest, redo = self._scan()
        if not redo:
            return None
        turn_id = redo[-1]
        note = "turn restored"
        if self.undo_journal is not None:
            if self.undo_journal.top_redo() == turn_id:
                res = self.undo_journal.redo()
                if res is not None:
                    note = f"{res.note}; turn restored"
            else:
                note = "files not journaled for this turn; turn restored"
        self.append("restored", {"turn_id": turn_id})
        return UndoResult(turn_id, note)

    def rewind(self, to_seq: int) -> int:
        """Undo every visible turn that started after ``to_seq``; returns how many."""
        started = {r.payload["turn_id"]: r.seq for r in self.records() if r.type == "turn_started"}
        count = 0
        for turn_id in reversed(self.turn_ids()):
            if started.get(turn_id, 0) <= to_seq:
                break
            self.undo()
            count += 1
        return count

    def fork(self, at_seq: int | None = None) -> Session:
        """Build a new session with this one's history up to ``at_seq``."""
        child_id = self.log._fork(self.id, at_seq, workdir=self.workdir, title=self.title)
        return Session(self.log, child_id, workdir=self.workdir, undo=self.undo_journal is not None)

    def export(self, fmt: str = "md") -> str:
        """Render the transcript as Markdown, HTML or JSON."""
        if fmt == "json":
            return json.dumps(
                {
                    "id": self.id,
                    "title": self.title,
                    "records": [r.__dict__ for r in self.records()],
                },
                ensure_ascii=False,
                indent=2,
            )
        parts: list[tuple[str, str]] = []
        for message in self.messages():
            if message.role == "user":
                parts.append(("You", message.text))
            elif message.role == "assistant":
                lines = [message.text] if message.text else []
                lines += [
                    f"→ {c.name}({json.dumps(c.args, ensure_ascii=False)[:200]})"
                    for c in message.tool_calls
                ]
                parts.append(("Luna", "\n".join(lines)))
            else:
                for r in message.results:
                    parts.append(
                        (f"{r.name or 'tool'}{' (error)' if r.is_error else ''}", r.text[:2000])
                    )
        if fmt == "md":
            body = "\n\n".join(f"## {who}\n\n{text}" for who, text in parts)
            return f"# {self.title or self.id}\n\n{body}\n"
        if fmt == "html":
            body = "\n".join(
                f"<section><h2>{html.escape(who)}</h2><pre>{html.escape(text)}</pre></section>"
                for who, text in parts
            )
            title = html.escape(self.title)
            return f'<!doctype html><meta charset="utf-8"><title>{title}</title>\n{body}\n'
        raise ValueError(f"unknown export format {fmt!r}")


__all__ = ["Entry", "EventLog", "Record", "Session", "SessionInfo", "patch_dangling"]
