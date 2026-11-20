"""The team board: runs, tasks, members and events of team runs in ``sessions.db``.

Lives in the same SQLite file as the session event log, on its own
connection (like :mod:`luna_core.assemblies`). Every write is one
``BEGIN IMMEDIATE`` transaction, so claiming a task is atomic across
threads and processes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

TASK_STATUSES = ("pending", "in_progress", "done", "failed", "blocked")
TASK_KINDS = ("code", "research")
RUN_STATUSES = ("planning", "awaiting_plan", "running", "finished")
OUTCOMES = ("done", "partial", "stopped", "stalled", "error")
MEMBER_STATUSES = ("idle", "working", "retired")
#: The lead's name on the board and in messages.
LEAD = "lead"

_DDL = """
CREATE TABLE IF NOT EXISTS team_runs(
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    workdir TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    stop_reason TEXT,
    session_id TEXT NOT NULL,
    branch TEXT,
    base TEXT,
    plan_json TEXT,
    settings_json TEXT NOT NULL,
    budget REAL,
    started REAL,
    finished REAL
);
CREATE TABLE IF NOT EXISTS team_tasks(
    run_id TEXT NOT NULL,
    id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    deps_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    assignee TEXT,
    note TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    branch TEXT,
    worktree TEXT,
    merge_of TEXT,
    commits_json TEXT NOT NULL DEFAULT '[]',
    updated REAL NOT NULL,
    PRIMARY KEY(run_id, id)
);
CREATE TABLE IF NOT EXISTS team_members(
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL,
    steps INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    busy REAL NOT NULL DEFAULT 0,
    hired REAL NOT NULL,
    PRIMARY KEY(run_id, name)
);
CREATE TABLE IF NOT EXISTS team_messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    text TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS team_messages_inbox ON team_messages(run_id, recipient, read);
CREATE TABLE IF NOT EXISTS team_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    member TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS team_events_run ON team_events(run_id, id);
"""

_RUN_COLS = (
    "id, created, updated, workdir, task, status, outcome, stop_reason, session_id, "
    "branch, base, plan_json, settings_json, budget, started, finished"
)
_TASK_COLS = (
    "run_id, id, seq, title, detail, kind, deps_json, status, assignee, note, attempts, "
    "branch, worktree, merge_of, commits_json, updated"
)
_MEMBER_COLS = "run_id, name, spec_json, status, steps, cost, busy, hired"
_EVENT_COLS = "id, run_id, ts, kind, member, data_json"
_RUN_FIELDS = frozenset(
    {"status", "outcome", "stop_reason", "branch", "base", "plan", "budget", "started", "finished"}
)
_TASK_FIELDS = frozenset(
    {"title", "detail", "status", "assignee", "note", "attempts", "branch", "worktree", "deps",
     "commits"}
)  # fmt: skip
_TASK_ID = re.compile(r"^T(\d+)$")


@dataclass(frozen=True)
class TeamRun:
    """One team run."""

    id: str
    created: float
    updated: float
    workdir: str
    task: str
    status: str
    outcome: str | None
    stop_reason: str | None
    session_id: str
    branch: str | None
    base: str | None
    plan: dict | None
    settings: dict
    budget: float | None
    started: float | None
    finished: float | None


@dataclass(frozen=True)
class TeamTask:
    """One task on the board."""

    run_id: str
    id: str
    seq: int
    title: str
    detail: str
    kind: str
    deps: tuple[str, ...]
    status: str
    assignee: str | None
    note: str
    attempts: int
    branch: str | None
    worktree: str | None
    merge_of: str | None
    commits: tuple[str, ...]
    updated: float


@dataclass(frozen=True)
class TeamMember:
    """A member (or the lead) of a run with its running totals."""

    run_id: str
    name: str
    spec: dict
    status: str
    steps: int
    cost: float
    busy: float
    hired: float


@dataclass(frozen=True)
class TeamEvent:
    """Something that happened in a run (also the protocol's ``team_*`` events)."""

    id: int
    run_id: str
    ts: float
    kind: str
    member: str
    data: dict


def _run(row: tuple) -> TeamRun:
    (rid, created, updated, workdir, task, status, outcome, stop_reason, session_id, branch,
     base, plan_json, settings_json, budget, started, finished) = row  # fmt: skip
    return TeamRun(
        rid,
        created,
        updated,
        workdir,
        task,
        status,
        outcome,
        stop_reason,
        session_id,
        branch,
        base,
        json.loads(plan_json) if plan_json else None,
        json.loads(settings_json),
        budget,
        started,
        finished,
    )


def _task(row: tuple) -> TeamTask:
    (run_id, tid, seq, title, detail, kind, deps_json, status, assignee, note, attempts, branch,
     worktree, merge_of, commits_json, updated) = row  # fmt: skip
    return TeamTask(
        run_id,
        tid,
        seq,
        title,
        detail,
        kind,
        tuple(json.loads(deps_json)),
        status,
        assignee,
        note,
        attempts,
        branch,
        worktree,
        merge_of,
        tuple(json.loads(commits_json)),
        updated,
    )


def _member(row: tuple) -> TeamMember:
    run_id, name, spec_json, status, steps, cost, busy, hired = row
    return TeamMember(run_id, name, json.loads(spec_json), status, steps, cost, busy, hired)


def _event(row: tuple) -> TeamEvent:
    eid, run_id, ts, kind, member, data_json = row
    return TeamEvent(eid, run_id, ts, kind, member, json.loads(data_json))


def _ready(task: TeamTask, by_id: dict[str, TeamTask]) -> bool:
    return task.status == "pending" and all(
        dep in by_id and by_id[dep].status == "done" for dep in task.deps
    )


class TeamStore:
    """Team runs over ``path`` (usually ``sessions.db``)."""

    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_DDL)
        self._lock = threading.Lock()
        self._listeners: list[Callable[[TeamEvent], None]] = []

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

    def create_run(
        self,
        *,
        workdir: str,
        task: str,
        session_id: str,
        settings: dict,
        budget: float | None = None,
    ) -> str:
        """Create a run in ``planning`` and return its id."""
        run_id = uuid.uuid4().hex[:8]
        now = time.time()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO team_runs(id, created, updated, workdir, task, status, session_id, "
                "settings_json, budget) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, now, now, workdir, task, "planning", session_id, json.dumps(settings),
                 budget),
            )  # fmt: skip
        return run_id

    def run(self, run_id: str) -> TeamRun | None:
        """Return one run or None."""
        rows = self._read(f"SELECT {_RUN_COLS} FROM team_runs WHERE id = ?", (run_id,))
        return _run(rows[0]) if rows else None

    def runs(
        self, *, workdir: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[TeamRun]:
        """Return runs, newest first, optionally of one directory and/or status."""
        where, args = [], []
        if workdir is not None:
            where.append("workdir = ?")
            args.append(workdir)
        if status is not None:
            where.append("status = ?")
            args.append(status)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        rows = self._read(
            f"SELECT {_RUN_COLS} FROM team_runs {clause}ORDER BY created DESC LIMIT ?",
            (*args, limit),
        )
        return [_run(r) for r in rows]

    def update_run(self, run_id: str, **values: object) -> None:
        """Change fields of a run (``plan`` is a dict)."""
        unknown = set(values) - _RUN_FIELDS
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")
        if "status" in values and values["status"] not in RUN_STATUSES:
            raise ValueError(f"run status must be one of {RUN_STATUSES}, got {values['status']!r}")
        if values.get("outcome") is not None and values["outcome"] not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {values['outcome']!r}")
        cols: dict[str, object] = {}
        for key, value in values.items():
            if key == "plan":
                cols["plan_json"] = None if value is None else json.dumps(value, ensure_ascii=False)
            else:
                cols[key] = value
        cols["updated"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self._write() as conn:
            cur = conn.execute(
                f"UPDATE team_runs SET {sets} WHERE id = ?", (*cols.values(), run_id)
            )
            if cur.rowcount == 0:
                raise KeyError(f"no team run {run_id}")

    def delete_run(self, run_id: str) -> None:
        """Forget a run and everything recorded for it."""
        with self._write() as conn:
            conn.execute("DELETE FROM team_runs WHERE id = ?", (run_id,))
            for table in ("team_tasks", "team_members", "team_messages", "team_events"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))

    # --- tasks ------------------------------------------------------------

    def _tasks_in(self, conn: sqlite3.Connection, run_id: str) -> list[TeamTask]:
        rows = conn.execute(
            f"SELECT {_TASK_COLS} FROM team_tasks WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [_task(r) for r in rows]

    def add_task(
        self,
        run_id: str,
        *,
        title: str,
        kind: str = "code",
        detail: str = "",
        deps: Iterable[str] = (),
        assignee: str | None = None,
        merge_of: str | None = None,
        task_id: str | None = None,
    ) -> TeamTask:
        """Add a pending task; its id is ``task_id`` or the next free ``T<n>``."""
        if kind not in TASK_KINDS:
            raise ValueError(f"task kind must be one of {TASK_KINDS}, got {kind!r}")
        if not title.strip():
            raise ValueError("a task needs a title")
        wanted = tuple(dict.fromkeys(deps))
        with self._write() as conn:
            rows = conn.execute(
                "SELECT id, seq FROM team_tasks WHERE run_id = ?", (run_id,)
            ).fetchall()
            ids = {r[0] for r in rows}
            missing = sorted(set(wanted) - ids)
            if missing:
                raise ValueError(f"unknown dependencies: {', '.join(missing)}")
            if task_id is None:
                numbers = [int(m.group(1)) for r in rows if (m := _TASK_ID.match(r[0]))]
                task_id = f"T{max(numbers, default=0) + 1}"
            elif task_id in ids:
                raise ValueError(f"task {task_id} already exists")
            seq = max((r[1] for r in rows), default=0) + 1
            conn.execute(
                "INSERT INTO team_tasks(run_id, id, seq, title, detail, kind, deps_json, status, "
                "assignee, merge_of, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, seq, title.strip(), detail, kind, json.dumps(list(wanted)),
                 "pending", assignee, merge_of, time.time()),
            )  # fmt: skip
        return self.task(run_id, task_id)

    def tasks(self, run_id: str) -> list[TeamTask]:
        """Every task of a run in board order."""
        with self._lock:
            return self._tasks_in(self._conn, run_id)

    def task(self, run_id: str, task_id: str) -> TeamTask | None:
        """One task or None."""
        rows = self._read(
            f"SELECT {_TASK_COLS} FROM team_tasks WHERE run_id = ? AND id = ?", (run_id, task_id)
        )
        return _task(rows[0]) if rows else None

    def ready(self, run_id: str) -> list[TeamTask]:
        """Pending tasks whose dependencies are all done."""
        tasks = self.tasks(run_id)
        by_id = {t.id: t for t in tasks}
        return [t for t in tasks if _ready(t, by_id)]

    @staticmethod
    def _take(conn: sqlite3.Connection, task: TeamTask, member: str) -> TeamTask:
        now = time.time()
        conn.execute(
            "UPDATE team_tasks SET status = 'in_progress', assignee = ?, attempts = attempts + 1, "
            "updated = ? WHERE run_id = ? AND id = ?",
            (member, now, task.run_id, task.id),
        )
        return dataclasses.replace(
            task, status="in_progress", assignee=member, attempts=task.attempts + 1, updated=now
        )

    def claim(self, run_id: str, task_id: str, member: str) -> bool:
        """Atomically take one ready task for ``member``; False when it is not available."""
        with self._write() as conn:
            by_id = {t.id: t for t in self._tasks_in(conn, run_id)}
            task = by_id.get(task_id)
            if task is None or not _ready(task, by_id) or task.assignee not in (None, member):
                return False
            self._take(conn, task, member)
            return True

    def claim_next(self, run_id: str, member: str) -> TeamTask | None:
        """Atomically take the next ready task for ``member`` (its own tasks first)."""
        with self._write() as conn:
            tasks = self._tasks_in(conn, run_id)
            by_id = {t.id: t for t in tasks}
            mine = [t for t in tasks if _ready(t, by_id) and t.assignee in (None, member)]
            if not mine:
                return None
            mine.sort(key=lambda t: (t.assignee != member, t.seq))
            return self._take(conn, mine[0], member)

    def update_task(self, run_id: str, task_id: str, **values: object) -> TeamTask:
        """Change fields of a task and return it (``deps`` / ``commits`` are lists)."""
        unknown = set(values) - _TASK_FIELDS
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        if "status" in values and values["status"] not in TASK_STATUSES:
            raise ValueError(
                f"task status must be one of {TASK_STATUSES}, got {values['status']!r}"
            )
        cols: dict[str, object] = {}
        for key, value in values.items():
            if key in ("deps", "commits"):
                cols[f"{key}_json"] = json.dumps(list(value))  # type: ignore[arg-type]
            else:
                cols[key] = value
        cols["updated"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self._write() as conn:
            cur = conn.execute(
                f"UPDATE team_tasks SET {sets} WHERE run_id = ? AND id = ?",
                (*cols.values(), run_id, task_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no task {task_id} in team {run_id}")
            row = conn.execute(
                f"SELECT {_TASK_COLS} FROM team_tasks WHERE run_id = ? AND id = ?",
                (run_id, task_id),
            ).fetchone()
        return _task(row)

    def remove_task(self, run_id: str, task_id: str, *, replace_with: Iterable[str] = ()) -> None:
        """Delete a task; tasks that depended on it depend on ``replace_with`` instead."""
        extra = list(dict.fromkeys(replace_with))
        with self._write() as conn:
            cur = conn.execute(
                "DELETE FROM team_tasks WHERE run_id = ? AND id = ?", (run_id, task_id)
            )
            if cur.rowcount == 0:
                raise KeyError(f"no task {task_id} in team {run_id}")
            rows = conn.execute(
                "SELECT id, deps_json FROM team_tasks WHERE run_id = ?", (run_id,)
            ).fetchall()
            for tid, deps_json in rows:
                deps = json.loads(deps_json)
                if task_id in deps:
                    new = [d for d in deps if d != task_id] + [e for e in extra if e not in deps]
                    conn.execute(
                        "UPDATE team_tasks SET deps_json = ? WHERE run_id = ? AND id = ?",
                        (json.dumps(new), run_id, tid),
                    )

    # --- members ----------------------------------------------------------

    def add_member(self, run_id: str, name: str, spec: dict) -> TeamMember:
        """Hire a member (idle); a second member with the same name is a ValueError."""
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT INTO team_members(run_id, name, spec_json, status, hired) "
                    "VALUES (?,?,?,?,?)",
                    (run_id, name, json.dumps(spec, ensure_ascii=False), "idle", time.time()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"member {name!r} is already on team {run_id}") from exc
        return self.member(run_id, name)

    def members(self, run_id: str, *, include_retired: bool = True) -> list[TeamMember]:
        """Members in hiring order."""
        rows = self._read(
            f"SELECT {_MEMBER_COLS} FROM team_members WHERE run_id = ? ORDER BY rowid", (run_id,)
        )
        members = [_member(r) for r in rows]
        return [m for m in members if include_retired or m.status != "retired"]

    def member(self, run_id: str, name: str) -> TeamMember | None:
        """One member or None."""
        rows = self._read(
            f"SELECT {_MEMBER_COLS} FROM team_members WHERE run_id = ? AND name = ?",
            (run_id, name),
        )
        return _member(rows[0]) if rows else None

    def update_member(
        self,
        run_id: str,
        name: str,
        *,
        status: str | None = None,
        add_steps: int = 0,
        add_cost: float = 0.0,
        add_busy: float = 0.0,
    ) -> None:
        """Set a member's status and add to its totals."""
        if status is not None and status not in MEMBER_STATUSES:
            raise ValueError(f"member status must be one of {MEMBER_STATUSES}, got {status!r}")
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE team_members SET status = COALESCE(?, status), steps = steps + ?, "
                "cost = cost + ?, busy = busy + ? WHERE run_id = ? AND name = ?",
                (status, add_steps, float(add_cost), float(add_busy), run_id, name),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no member {name} in team {run_id}")

    # --- events -----------------------------------------------------------

    def log_event(self, run_id: str, kind: str, member: str = "", **data: object) -> TeamEvent:
        """Record an event and tell the subscribers."""
        ts = time.time()
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO team_events(run_id, ts, kind, member, data_json) VALUES (?,?,?,?,?)",
                (run_id, ts, kind, member, json.dumps(data, ensure_ascii=False, default=str)),
            )
            event = TeamEvent(cur.lastrowid, run_id, ts, kind, member, dict(data))
        for fn in list(self._listeners):
            with contextlib.suppress(Exception):
                fn(event)
        return event

    def events(
        self, run_id: str, *, after: int = 0, kinds: Iterable[str] | None = None
    ) -> list[TeamEvent]:
        """Events of a run after id ``after``, oldest first, optionally of some kinds."""
        rows = self._read(
            f"SELECT {_EVENT_COLS} FROM team_events WHERE run_id = ? AND id > ? ORDER BY id",
            (run_id, after),
        )
        wanted = None if kinds is None else set(kinds)
        return [_event(r) for r in rows if wanted is None or r[3] in wanted]

    def subscribe(self, fn: Callable[[TeamEvent], None]) -> Callable[[], None]:
        """Call ``fn`` with every new event of this store; returns the unsubscribe function."""
        self._listeners.append(fn)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(fn)

        return unsubscribe

    def fingerprint(self, run_id: str) -> str:
        """Return a hash of the tasks and members (not messages) for the stall detector."""
        tasks = [(t.id, t.status, t.assignee, t.attempts, list(t.deps)) for t in self.tasks(run_id)]
        members = [(m.name, m.status) for m in self.members(run_id)]
        return hashlib.sha256(json.dumps([tasks, members]).encode()).hexdigest()

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()
