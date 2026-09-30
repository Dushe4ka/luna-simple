# Luna TUI Commands + Turn Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every REPL slash command works in the TUI, and a TUI turn runs the same pipeline as a REPL turn, with model / provider / plan / pinned files / usage isolated and persisted per session.

**Architecture:** Per-session state lives in a new `luna_session_state` table. The server keeps one `SessionRuntime` (own agent, lock, turn phase) per session. The REPL's before/after-turn logic moves into `luna/turn/engine.py`, which both the REPL loop and the server call. Slash commands move into a transport-neutral registry (`luna/commands/`) that returns structured `CommandResult`s. The REPL renders them to the console, and the server exposes them via `POST /sessions/{id}/command`. The TUI runs turns in a Textual worker so read-only commands work mid-turn.

**Tech Stack:** Python 3.12, Textual 8.2.8, Starlette + sse-starlette (`starlette.concurrency.iterate_in_threadpool` / `run_in_threadpool`), sqlite3, LangGraph, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-30-luna-tui-commands-design.md`

## Global Constraints

- `/model`, `/provider`, `/plan` change **only the current session**; persisted in `luna_session_state`.
- The undo `session_id` of a session is its `thread_id` (same as the REPL today).
- One streaming turn per session: `/messages` or `/approve` while `runtime.lock.locked()` → `409 {"error": "session_busy"}`. `POST /command` for a non-`read` command while locked → same 409.
- `POST /command` with a `ui` command → `400 {"error": "client_command"}`. A cached runtime asked for another workdir → `400 {"error": "workdir_mismatch"}`.
- Every existing user-visible REPL string stays byte-identical (tests assert on them). New TUI-only strings are Russian.
- Notice levels: `dim | info | ok | warn | error`. REPL styles: `dim`/`ok` → `"dim"`, `info` → `PALETTE["blue"]`, `warn` → `"yellow"`, `error` → `PALETTE["mauve"]`. TUI styles: `dim`/`ok` → `$moon-dim`, `info` → `$peri`, `warn` → `#e8c37a`, `error` → `$err`.
- Runtime registry capacity: `8` sessions (LRU; never evict a locked runtime).
- Blocking work on the server (agent build, `iter_turn`, engine steps, commands) runs off the event loop (`iterate_in_threadpool` / `run_in_threadpool`).
- `create_app(agent_factory=...)` (per-workdir, used by 23 test call sites) keeps working; production uses the new `session_agent_factory=`.
- Run tests: `uv run pytest -o addopts="" -q` (the default addopts hide the summary). Lint: `uv run ruff check luna tests`.
- Every task ends green on the **whole** suite, not just its own file.

## Review Focus

1. **Server restarted (or runtime evicted) while a turn waits for approval.** `/approve` must still resume the graph and finish (no `prepared` in memory) instead of crashing → Task 6 test `test_approve_after_runtime_loss_still_finishes`.
2. **TUI closed mid-approval, reopened, new message sent.** The session must not stay "busy" forever: busy means *streaming now* (`lock.locked()`), not "phase != idle" → Task 6 test `test_new_message_after_abandoned_approval_is_accepted`.
3. **Two sessions in one folder.** Plan mode, model, pinned files and undo journal of one must never leak into the other → Task 6 test `test_two_sessions_in_one_workdir_are_isolated`.
4. **A failing `/model` or `/provider` (bad name, missing key).** The session must keep working on its previous model and the stored state must not change → Task 8 tests.
5. **Read command during a long turn.** It must answer immediately, not after the turn (the server event loop must not be blocked by the sync graph) → Task 11 test `test_read_command_answers_while_a_turn_streams`.

---

## File Structure

| File | Responsibility |
|---|---|
| `luna/core/session_state.py` (new) | `SessionState` dataclass + `SessionStateStore` (sqlite, best-effort) |
| `luna/turn/engine.py` (new) | `Notice`, `PreparedTurn`, `TurnOutcome`, `FinishResult`, `prepare_turn`, `format_and_diagnose`, `verify_step`, `fixup_prompt`, `finish_turn`, `finish_fixup` |
| `luna/core/session.py` | REPL loop + `run_once` call the engine; `_format_and_diagnose` / `_run_verification` become thin wrappers; `compact_history` extracted |
| `luna/server/runtime.py` (new) | `SessionRuntime`, `RuntimeRegistry`, `WorkdirMismatch`, `runtime_for()` |
| `luna/server/run.py` | `make_session_agent_factory`, `run_serve` wiring |
| `luna/server/app.py` | `session_agent_factory=`, registry on `app.state`, new routes |
| `luna/server/turns.py`, `approvals.py` | turn flow via runtime + engine, threadpool streaming, notices, fix-up |
| `luna/commands/__init__.py`, `base.py`, `registry.py`, `builtin.py` (new) | registry, result types, `CommandEnv`, all command handlers |
| `luna/repl/commands.py` | REPL adapter (`CommandContext`, `DispatchResult`, `dispatch`, `HELP`) over the registry |
| `luna/server/commands.py` (new) | `POST /command`, `GET /commands`, `GET /state` |
| `luna/server/client.py` | `run_command`, `list_commands`, `get_state`, new error texts |
| `luna/tui/pickers.py` (new) | `ChoiceModal`, `ConfirmModal` |
| `luna/tui/chat.py` | `submit()`, command routing, `NoticeRow`, worker turns, busy rules, state refresh |
| `luna/tui/commands.py`, `status_bar.py`, `app.py`, `luna.tcss` | autocomplete source, status bar fields, plan border |

---

### Task 1: `SessionState` and `SessionStateStore`

**Files:**
- Create: `luna/core/session_state.py`
- Test: `tests/test_session_state.py`

**Interfaces:**
- Consumes: `luna.core.persistence._db_path`, `_INDEX_ERRORS`; `luna.turn.context.PinnedFiles`; `luna.config.usage.SessionUsage`, `TurnUsage`.
- Produces:
  - `SessionState(thread_id: str, workdir: str, provider: str | None = None, model: str | None = None, plan: bool = False, pinned: PinnedFiles = PinnedFiles(), pending_diagnostics: str = "", usage: SessionUsage = SessionUsage())`
  - `SessionStateStore(env=None)` with `.ok`, `.load(thread_id: str, workdir: str) -> SessionState`, `.save(state: SessionState) -> None`

- [ ] **Step 1: Write the failing tests** — `tests/test_session_state.py`:

```python
from luna.config.usage import TurnUsage
from luna.core.session_state import SessionState, SessionStateStore


def test_unknown_session_loads_defaults(tmp_path):
    state = SessionStateStore().load("t1", str(tmp_path))
    assert state == SessionState("t1", str(tmp_path))
    assert state.plan is False and state.pinned.paths == [] and state.usage.turns == []


def test_every_field_round_trips(tmp_path):
    store = SessionStateStore()
    state = store.load("t1", str(tmp_path))
    state.provider, state.model, state.plan = "openai", "gpt-5", True
    state.pinned.add("a.py", "b.py")
    state.pending_diagnostics = "a.py:1 E501"
    state.usage.add_turn(TurnUsage(10, 5, 15))
    store.save(state)

    again = SessionStateStore().load("t1", str(tmp_path))
    assert (again.provider, again.model, again.plan) == ("openai", "gpt-5", True)
    assert again.pinned.paths == ["a.py", "b.py"]
    assert again.pending_diagnostics == "a.py:1 E501"
    assert again.usage.totals == (10, 5, 15)


def test_sessions_do_not_share_state(tmp_path):
    store = SessionStateStore()
    one = store.load("t1", str(tmp_path))
    one.plan = True
    store.save(one)
    assert store.load("t2", str(tmp_path)).plan is False


def test_degrades_to_defaults_when_the_db_cannot_open(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker / "cfg"))
    store = SessionStateStore()
    assert store.ok is False
    state = store.load("t1", str(tmp_path))
    state.plan = True
    store.save(state)  # no-op, no raise
    assert store.load("t1", str(tmp_path)).plan is False
```

- [ ] **Step 2: Run** `uv run pytest -o addopts="" -q tests/test_session_state.py` — Expected: FAIL, `ModuleNotFoundError: No module named 'luna.core.session_state'`.

- [ ] **Step 3: Implement `luna/core/session_state.py`**

```python
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
        """Stored state for ``thread_id``, or defaults when unknown/unavailable."""
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
```

- [ ] **Step 4: Run** `uv run pytest -o addopts="" -q tests/test_session_state.py` — Expected: `4 passed`. Then full suite — Expected: all pass.

- [ ] **Step 5: Commit** — `git add luna/core/session_state.py tests/test_session_state.py && git commit -m "feat: per-session state (model, provider, plan, pinned, usage) persisted in sessions.db"`

---

### Task 2: Turn engine — `prepare_turn` and `Notice`

**Files:**
- Create: `luna/turn/engine.py`
- Test: `tests/test_turn_engine.py`

**Interfaces:**
- Consumes: `SessionState` (Task 1); `luna.turn.undo.begin_turn`, `luna.turn.gitinfo.dirty_paths/is_git_repo`, `luna.turn.context.expand_mentions/render_pinned`.
- Produces:
  - `MUTATING: frozenset[str]`, `AT_AGENT_RE`
  - `Notice(level: str, text: str)` (frozen dataclass)
  - `PreparedTurn(content: str, title_line: str, dirty_before: list[str])`
  - `delegate_line(line: str, subagent_names: set[str]) -> str`
  - `prepare_turn(state, line, *, agent, thread_id, workdir, session_id, subagent_names) -> PreparedTurn`

- [ ] **Step 1: Write the failing tests** — `tests/test_turn_engine.py`:

```python
from types import SimpleNamespace

from luna.core.session_state import SessionState
from luna.turn.engine import PreparedTurn, delegate_line, prepare_turn


class _Agent:
    def get_state(self, config):
        return SimpleNamespace(values={"messages": []})


def _prepare(state, line, tmp_path, subagents=frozenset()):
    return prepare_turn(
        state,
        line,
        agent=_Agent(),
        thread_id=state.thread_id,
        workdir=str(tmp_path),
        session_id=state.thread_id,
        subagent_names=set(subagents),
    )


def test_plain_line_passes_through(tmp_path):
    prepared = _prepare(SessionState("t1", str(tmp_path)), "hello", tmp_path)
    assert isinstance(prepared, PreparedTurn)
    assert prepared.content == "hello" and prepared.title_line == "hello"


def test_pinned_files_are_prepended_fresh_from_disk(tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n")
    state = SessionState("t1", str(tmp_path))
    state.pinned.add("a.py")
    prepared = _prepare(state, "look", tmp_path)
    assert "print(1)" in prepared.content
    assert prepared.content.rstrip().endswith("look")


def test_pending_diagnostics_are_sent_once_then_cleared(tmp_path):
    state = SessionState("t1", str(tmp_path), pending_diagnostics="a.py:1 E501")
    first = _prepare(state, "go", tmp_path)
    assert first.content.startswith("<diagnostics>\na.py:1 E501\n</diagnostics>")
    assert state.pending_diagnostics == ""
    assert "<diagnostics>" not in _prepare(state, "again", tmp_path).content


def test_at_agent_delegates_only_for_known_subagents(tmp_path):
    assert delegate_line("@researcher find X", {"researcher"}).startswith(
        "Delegate this to the 'researcher' subagent"
    )
    assert delegate_line("@nobody find X", {"researcher"}) == "@nobody find X"
    prepared = _prepare(SessionState("t1", str(tmp_path)), "@researcher find X", tmp_path, {"researcher"})
    assert prepared.title_line.startswith("Delegate this to the 'researcher'")


def test_at_file_mentions_are_expanded(tmp_path):
    (tmp_path / "notes.md").write_text("secret sauce\n")
    prepared = _prepare(SessionState("t1", str(tmp_path)), "read @notes.md", tmp_path)
    assert "secret sauce" in prepared.content
```

- [ ] **Step 2: Run** `uv run pytest -o addopts="" -q tests/test_turn_engine.py` — Expected: FAIL, `ModuleNotFoundError: No module named 'luna.turn.engine'`.

- [ ] **Step 3: Implement the first half of `luna/turn/engine.py`**

```python
"""The before/after-turn pipeline shared by the REPL and the local server.

Streaming stays transport-specific (the REPL renders to a console and asks
for approvals inline; the server streams SSE and resumes via ``/approve``),
but everything around it lives here once: ``@agent`` delegation, ``@file``
expansion, pinned files, carried-over diagnostics and the undo checkpoint
before a turn; usage, the session index, format + diagnose, verify with one
fix-up turn and auto-reload after it. Functions return :class:`Notice`s
instead of printing, so each surface renders them its own way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from luna.core.session_state import SessionState
from luna.turn import gitinfo, undo
from luna.turn.context import expand_mentions, render_pinned

#: Tool names whose use marks a turn as mutating and triggers format/verify.
MUTATING = frozenset({"write_file", "edit_file", "delete", "execute"})

#: Matches a line invoking a subagent by name, e.g. ``@researcher do X``.
AT_AGENT_RE = re.compile(r"^@([\w-]+)\s+(.+)$", re.DOTALL)


@dataclass(frozen=True)
class Notice:
    """One short status line for the user: ``level`` is dim/info/ok/warn/error."""

    level: str
    text: str


@dataclass
class PreparedTurn:
    """What :func:`prepare_turn` hands to the streaming layer."""

    content: str
    title_line: str
    dirty_before: list[str]


def delegate_line(line: str, subagent_names: set[str]) -> str:
    """Rewrite ``@name text`` into a delegation request when ``name`` is a subagent."""
    match = AT_AGENT_RE.match(line)
    if match and match.group(1) in subagent_names:
        return (
            f"Delegate this to the '{match.group(1)}' subagent using the "
            f"task tool: {match.group(2)}"
        )
    return line


def prepare_turn(
    state: SessionState,
    line: str,
    *,
    agent,
    thread_id: str,
    workdir: str,
    session_id: str,
    subagent_names: set[str],
) -> PreparedTurn:
    """Build the message content for a turn and checkpoint files for undo."""
    line = delegate_line(line, subagent_names)
    config = {"configurable": {"thread_id": thread_id}}
    try:
        current_messages = agent.get_state(config).values.get("messages", [])
    except Exception:  # noqa: BLE001 - a stub/broken agent must not block the turn
        current_messages = []
    undo.begin_turn(workdir, session_id, len(current_messages))
    # captured *after* begin_turn so its own journal writes don't register as
    # "newly dirty" when .luna/ isn't gitignored
    dirty_before = gitinfo.dirty_paths(workdir) if gitinfo.is_git_repo(workdir) else []
    diag_block = (
        f"<diagnostics>\n{state.pending_diagnostics}\n</diagnostics>\n\n"
        if state.pending_diagnostics
        else ""
    )
    state.pending_diagnostics = ""
    pinned_block = render_pinned(state.pinned, workdir)
    content = (
        diag_block
        + (pinned_block + "\n\n" if pinned_block else "")
        + expand_mentions(line, workdir)
    )
    return PreparedTurn(content=content, title_line=line, dirty_before=dirty_before)
```

- [ ] **Step 4: Run** the new tests — Expected: `5 passed`; full suite green.

- [ ] **Step 5: Commit** — `git commit -m "feat: turn engine prepare_turn shared by REPL and server"` (add both files).

---

### Task 3: Turn engine — finish, format/diagnose, verify; REPL wrappers delegate

**Files:**
- Modify: `luna/turn/engine.py` (append)
- Modify: `luna/core/session.py` (`_format_and_diagnose`, `_run_verification` become wrappers; new `_print_notices`; `compact_thread` split into `compact_history` + printing)
- Test: `tests/test_turn_engine.py` (append); existing `tests/test_verify.py` must stay green unchanged

**Interfaces:**
- Consumes: Task 2.
- Produces:
  - `TurnOutcome(usage: TurnUsage = TurnUsage(), tool_names: set[str] = set(), reload_requested: bool = False)`
  - `FinishResult(notices: list[Notice], fixup_prompt: str | None, reload: bool)`
  - `format_and_diagnose(cfg, before: list[str] | None) -> tuple[list[Notice], str]`
  - `verify_step(cfg) -> tuple[bool, str, list[Notice]]` (ok, tail, notices)
  - `fixup_prompt(cfg, tail: str) -> str`
  - `finish_turn(state, prepared, outcome, *, cfg, index, thread_id, workdir) -> FinishResult`
  - `finish_fixup(cfg) -> list[Notice]`
  - `luna.core.session.compact_history(agent, thread_id) -> tuple[bool, str]`
  - `luna.core.session._print_notices(console, notices) -> None`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_turn_engine.py`:

```python
from luna.config.config import LunaConfig
from luna.config.usage import TurnUsage
from luna.turn import engine
from luna.turn.engine import FinishResult, Notice, TurnOutcome, finish_fixup, finish_turn


class _Index:
    def __init__(self):
        self.recorded, self.touched = [], []

    def record(self, thread_id, workdir, title):
        self.recorded.append((thread_id, workdir, title))

    def touch(self, thread_id):
        self.touched.append(thread_id)


def _finish(state, outcome, cfg, tmp_path, index=None):
    prepared = PreparedTurn(content="x", title_line="fix the bug", dirty_before=[])
    return finish_turn(
        state, prepared, outcome, cfg=cfg, index=index, thread_id="t1", workdir=str(tmp_path)
    )


def test_read_only_turn_records_usage_and_index_without_verify(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "run_verify", lambda *a: (_ for _ in ()).throw(AssertionError))
    state, index = SessionState("t1", str(tmp_path)), _Index()
    cfg = LunaConfig(workdir=str(tmp_path), verify_command="pytest")
    result = _finish(state, TurnOutcome(usage=TurnUsage(10, 2, 12)), cfg, tmp_path, index)
    assert result == FinishResult(notices=[], fixup_prompt=None, reload=False)
    assert state.usage.totals == (10, 2, 12)
    assert index.recorded == [("t1", str(tmp_path), "fix the bug")] and index.touched == ["t1"]


def test_mutating_turn_with_failing_verify_asks_for_one_fixup(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "run_verify", lambda cmd, wd: (False, "1 failed"))
    monkeypatch.setattr(engine, "format_and_diagnose", lambda cfg, before: ([], "a.py:1 E1"))
    state = SessionState("t1", str(tmp_path))
    cfg = LunaConfig(workdir=str(tmp_path), verify_command="pytest")
    result = _finish(state, TurnOutcome(tool_names={"edit_file"}), cfg, tmp_path)
    assert result.fixup_prompt == "The verify command `pytest` failed. Output:\n1 failed\nFix it."
    assert Notice("warn", "verify failed\n1 failed") in result.notices
    assert state.pending_diagnostics == "a.py:1 E1"


def test_passing_verify_reports_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "run_verify", lambda cmd, wd: (True, ""))
    monkeypatch.setattr(engine, "format_and_diagnose", lambda cfg, before: ([], ""))
    cfg = LunaConfig(workdir=str(tmp_path), verify_command="pytest")
    result = _finish(SessionState("t1", str(tmp_path)), TurnOutcome(tool_names={"execute"}), cfg, tmp_path)
    assert result.notices == [Notice("ok", "✓ verify ok")] and result.fixup_prompt is None


def test_reload_request_is_passed_through(tmp_path):
    result = _finish(
        SessionState("t1", str(tmp_path)),
        TurnOutcome(reload_requested=True),
        LunaConfig(workdir=str(tmp_path)),
        tmp_path,
    )
    assert result.reload is True


def test_finish_fixup_reports_the_retry_result(tmp_path, monkeypatch):
    cfg = LunaConfig(workdir=str(tmp_path), verify_command="pytest")
    monkeypatch.setattr(engine, "run_verify", lambda cmd, wd: (False, "still red"))
    assert finish_fixup(cfg) == [Notice("warn", "⚠ verify still failing after 1 retry\nstill red")]
    monkeypatch.setattr(engine, "run_verify", lambda cmd, wd: (True, ""))
    assert finish_fixup(cfg) == [Notice("ok", "✓ verify ok")]
```

- [ ] **Step 2: Run** them — Expected: FAIL, `ImportError: cannot import name 'FinishResult'`.

- [ ] **Step 3: Append to `luna/turn/engine.py`** (move the body of `luna/core/session.py::_format_and_diagnose` here, emitting notices instead of printing; add imports `from dataclasses import field`, `from luna.config.usage import TurnUsage`, `from luna.core.persistence import make_title`, `from luna.turn import diagnose, fmt`, `from luna.turn.verify import run_verify`):

```python
@dataclass
class TurnOutcome:
    """What the streaming layer observed during a turn."""

    usage: TurnUsage = field(default_factory=TurnUsage)
    tool_names: set[str] = field(default_factory=set)
    reload_requested: bool = False


@dataclass
class FinishResult:
    """What happens after a turn: notices, an optional fix-up turn, a reload."""

    notices: list[Notice]
    fixup_prompt: str | None
    reload: bool


def format_and_diagnose(cfg, before: list[str] | None) -> tuple[list[Notice], str]:
    """Format then diagnose the files this turn changed. Returns (notices, diagnose text).

    ``before`` is the ``dirty_paths`` snapshot captured before the turn ran; only
    paths that became newly dirty during the turn are passed to the format/
    diagnose commands, so a file the user had already changed before this turn
    started is left alone. ``None`` falls back to the whole-repo behavior.
    """
    notices: list[Notice] = []
    before_set = set(before) if before is not None else None
    is_repo = gitinfo.is_git_repo(cfg.workdir)

    def _touched_now() -> list[str]:
        if not is_repo:
            return []
        current = gitinfo.dirty_paths(cfg.workdir)
        return current if before_set is None else [p for p in current if p not in before_set]

    changed = _touched_now()
    if is_repo and before_set is not None and not changed:
        return notices, ""
    fmt_cmd = cfg.format_command
    if fmt_cmd == "auto":
        fmt_cmd = fmt.detect(cfg.workdir)
    if fmt_cmd:
        touched = fmt.run(fmt_cmd, cfg.workdir, changed)
        if touched:
            notices.append(Notice("dim", f"⌁ formatted {len(touched)} file(s)"))
    diag_cmd = cfg.diagnose_command
    if diag_cmd == "auto":
        diag_cmd = diagnose.detect(cfg.workdir)
    if not diag_cmd:
        return notices, ""
    changed = _touched_now()
    if is_repo and before_set is not None and not changed:
        # formatting can normalize a turn's edit back to the committed content
        return notices, ""
    text = diagnose.run(diag_cmd, cfg.workdir, changed)
    if text:
        notices.append(Notice("dim", text))
    return notices, text


def verify_step(cfg) -> tuple[bool, str, list[Notice]]:
    """Run the verify command once: (ok, output tail, notices)."""
    ok, tail = run_verify(cfg.verify_command, cfg.workdir)
    if ok:
        return True, "", [Notice("ok", "✓ verify ok")]
    return False, tail, [Notice("warn", f"verify failed\n{tail}")]


def fixup_prompt(cfg, tail: str) -> str:
    """The message for the single automatic fix-up turn after a failed verify."""
    return f"The verify command `{cfg.verify_command}` failed. Output:\n{tail}\nFix it."


def finish_turn(
    state: SessionState,
    prepared: PreparedTurn,
    outcome: TurnOutcome,
    *,
    cfg,
    index,
    thread_id: str,
    workdir: str,
) -> FinishResult:
    """Everything after a turn completed without a pending approval."""
    state.usage.add_turn(outcome.usage)
    notices: list[Notice] = []
    fix = None
    if outcome.tool_names & MUTATING:
        fmt_notices, diagnostics = format_and_diagnose(cfg, prepared.dirty_before)
        notices += fmt_notices
        state.pending_diagnostics = diagnostics
        if cfg.verify_command:
            ok, tail, verify_notices = verify_step(cfg)
            notices += verify_notices
            if not ok:
                fix = fixup_prompt(cfg, tail)
    if index is not None:
        index.record(thread_id, workdir, make_title(prepared.title_line))
        index.touch(thread_id)
    return FinishResult(notices=notices, fixup_prompt=fix, reload=outcome.reload_requested)


def finish_fixup(cfg) -> list[Notice]:
    """Re-run verify once after the fix-up turn."""
    ok, tail = run_verify(cfg.verify_command, cfg.workdir)
    if ok:
        return [Notice("ok", "✓ verify ok")]
    return [Notice("warn", f"⚠ verify still failing after 1 retry\n{tail}")]
```

- [ ] **Step 4: Turn the REPL helpers in `luna/core/session.py` into wrappers**

Add `from luna.turn import engine` and replace the bodies (keep names and signatures — `tests/test_verify.py` calls them):

```python
_NOTICE_STYLE = {
    "dim": "dim",
    "ok": "dim",
    "info": PALETTE["blue"],
    "warn": "yellow",
    "error": PALETTE["mauve"],
}


def _print_notices(console: Console, notices) -> None:
    """Render engine notices the way the REPL always printed these lines."""
    for notice in notices:
        console.print(notice.text, style=_NOTICE_STYLE.get(notice.level, ""), markup=False)


def _format_and_diagnose(console: Console, cfg: LunaConfig, before: list[str] | None = None) -> str:
    """Format then diagnose the files this turn changed (see :func:`engine.format_and_diagnose`)."""
    notices, text = engine.format_and_diagnose(cfg, before)
    _print_notices(console, notices)
    return text


def _run_verification(
    agent, turn_config: dict, console: Console, cfg, input_fn, rules=None
) -> None:
    """Run the verify command; on failure, take exactly one fix-up turn."""
    if not cfg.verify_command:
        return
    ok, tail, notices = engine.verify_step(cfg)
    _print_notices(console, notices)
    if ok:
        return
    payload = {"messages": [{"role": "user", "content": engine.fixup_prompt(cfg, tail)}]}
    _stream_turn_resilient(
        agent, payload, turn_config, console, input_fn, rules=rules, workdir=cfg.workdir
    )
    _print_notices(console, engine.finish_fixup(cfg))
```

Split `compact_thread` (keep its signature — `/compact` in the REPL and tests use it):

```python
def compact_history(agent, thread_id: str) -> tuple[bool, str]:
    """Replace this thread's history with a model-written summary: (ok, message)."""
    config = {"configurable": {"thread_id": thread_id}}
    pre = agent.get_state(config).values.get("messages", [])
    result = agent.invoke({"messages": [{"role": "user", "content": _COMPACT_ASK}]}, config)
    if isinstance(result, dict) and result.get("__interrupt__"):
        result = agent.invoke(
            Command(resume={"decisions": [{"type": "reject", "message": "summary only"}]}),
            config,
        )
    summary = ""
    messages = result.get("messages", []) if isinstance(result, dict) else []
    for msg in reversed(messages):
        text = getattr(msg, "content", "")
        if getattr(msg, "type", "") == "ai" and isinstance(text, str) and text.strip():
            summary = text.strip()
            break
    if not summary:
        added = agent.get_state(config).values["messages"][len(pre) :]
        agent.update_state(config, {"messages": [RemoveMessage(id=m.id) for m in added]})
        return False, "/compact: no summary produced"
    agent.update_state(
        config,
        {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                HumanMessage(id=uuid.uuid4().hex, content="[compacted] Handoff note:\n" + summary),
            ]
        },
    )
    return True, "compacted — history replaced with a summary"


def compact_thread(agent, thread_id: str, console: Console) -> None:
    """Replace this thread's message history with a model-written summary, in place."""
    ok, message = compact_history(agent, thread_id)
    console.print(f"[{PALETTE['blue'] if ok else PALETTE['mauve']}]{message}[/]")
```

Delete the now-unused module constant `_MUTATING`/`_AT_AGENT_RE` in `session.py` **only after** Task 4 switches `run_repl`; for now leave them.

- [ ] **Step 5: Run** `uv run pytest -o addopts="" -q tests/test_turn_engine.py tests/test_verify.py tests/test_session.py tests/test_repl_flow.py` — Expected: all pass. Full suite green.

- [ ] **Step 6: Commit** — `git commit -m "feat: turn engine finish/verify/fix-up; REPL helpers delegate to it"`

---

### Task 4: REPL loop runs on the engine

**Files:**
- Modify: `luna/core/session.py` (`run_repl` turn block)
- Test: existing `tests/test_repl_flow.py`, `tests/test_session.py`, `tests/test_verify.py` (no edits) + one new test in `tests/test_repl_flow.py`

**Interfaces:**
- Consumes: Tasks 1–3 (`SessionState` in memory, engine functions).

- [ ] **Step 1: Write the failing test** — append to `tests/test_repl_flow.py` (it guards that the REPL now keeps pending diagnostics in a `SessionState`, visible through the engine):

```python
def test_repl_uses_the_shared_engine(monkeypatch, tmp_path):
    """run_repl must build turns through luna.turn.engine.prepare_turn."""
    from rich.console import Console
    import io

    from luna.core import session as session_mod

    calls = []
    real = session_mod.engine.prepare_turn

    def spy(*a, **k):
        calls.append(a[1])
        return real(*a, **k)

    monkeypatch.setattr(session_mod.engine, "prepare_turn", spy)

    class _Agent:
        def get_state(self, config):
            from types import SimpleNamespace
            return SimpleNamespace(values={"messages": []})

    lines = iter(["hi"])

    def _input(_prompt):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(session_mod, "_stream_turn_resilient", lambda *a, **k: ("", False, session_mod.TurnUsage(), set()))
    session_mod.run_repl(_Agent(), console=Console(file=io.StringIO()), input_fn=_input, workdir=str(tmp_path))
    assert calls == ["hi"]
```

- [ ] **Step 2: Run** it — Expected: FAIL (`calls == []`).

- [ ] **Step 3: Replace the inline turn block in `run_repl`**

At the top of `run_repl`, after `pinned = PinnedFiles()` and `session_usage = SessionUsage()`, create the shared state object (REPL: in memory only):

```python
    state = SessionState(thread_id, workdir, pinned=pinned, usage=session_usage)
```

(import `from luna.core.session_state import SessionState`). Remove the `pending_diagnostics = ""` local. Replace everything from `at_match = _AT_AGENT_RE.match(line)` down to (and including) the `index.record/index.touch` block with:

```python
        turn_config = {"configurable": {"thread_id": thread_id}}
        state.thread_id = thread_id
        prepared = engine.prepare_turn(
            state,
            line,
            agent=agent,
            thread_id=thread_id,
            workdir=workdir,
            session_id=session_id,
            subagent_names=subagent_names,
        )
        payload = {"messages": [{"role": "user", "content": prepared.content}]}
        try:
            _, reload_requested, turn_usage, tool_names = _stream_turn_resilient(
                agent, payload, turn_config, console, input_fn, rules=rules, workdir=workdir
            )
        except KeyboardInterrupt:
            console.print(f"\n[{PALETTE['mauve']}]turn cancelled[/]")
            continue
        except Exception as exc:  # noqa: BLE001 - a provider/network/persistence error must not kill the session
            console.print(f"[{PALETTE['mauve']}]turn failed: {exc}[/]")
            continue
        before = len(session_usage.turns)
        result = engine.finish_turn(
            state,
            prepared,
            engine.TurnOutcome(usage=turn_usage, tool_names=tool_names),
            cfg=config,
            index=index,
            thread_id=thread_id,
            workdir=workdir,
        )
        if len(session_usage.turns) > before:
            indicator = indicator_line(session_usage, config.provider, config.model, config.pricing)
            console.print(f"[dim]{indicator}[/]")
        _print_notices(console, result.notices)
        if result.fixup_prompt is not None:
            try:
                fix_payload = {"messages": [{"role": "user", "content": result.fixup_prompt}]}
                _stream_turn_resilient(
                    agent, fix_payload, turn_config, console, input_fn, rules=rules, workdir=workdir
                )
                _print_notices(console, engine.finish_fixup(config))
            except KeyboardInterrupt:
                console.print(f"\n[{PALETTE['mauve']}]verify fix-up cancelled[/]")
```

Keep the existing auto-reload block (`if reload_requested and rebuild is not None: …`) right after it, unchanged. Delete `_MUTATING` and `_AT_AGENT_RE` from `session.py` if nothing else references them (`grep -n "_MUTATING\|_AT_AGENT_RE" luna`); `run_once` switches to `engine.MUTATING`.

- [ ] **Step 4: Run** `uv run pytest -o addopts="" -q tests/test_repl_flow.py tests/test_session.py tests/test_verify.py tests/test_persistence.py` then the full suite — Expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "refactor: REPL turn loop runs on the shared turn engine"`

---

### Task 5: `SessionRuntime`, `RuntimeRegistry` and per-session agents

**Files:**
- Create: `luna/server/runtime.py`
- Modify: `luna/server/run.py` (add `make_session_agent_factory`, wire in `run_serve`), `luna/server/app.py` (`session_agent_factory`, `app.state.runtimes`)
- Test: `tests/test_server_runtime.py`; update `tests/test_server_trust.py::test_run_serve_wires_the_real_trust_check` to also assert `captured["session_agent_factory"] is not None`

**Interfaces:**
- Consumes: `SessionState`, `SessionStateStore` (Task 1); `TurnOutcome`, `PreparedTurn` (Tasks 2–3).
- Produces:
  - `SessionRuntime(state, *, store, build_agent)` — attrs `thread_id`, `workdir`, `state`, `phase` (`"idle"|"turn"|"fixup"`), `prepared: PreparedTurn | None`, `outcome: TurnOutcome`, `lock: asyncio.Lock`, `reload_after_turn: bool`; property `agent`; methods `config() -> LunaConfig`, `rebuild() -> None`, `switch_model(model: str) -> None`, `switch_provider(provider: str) -> None`, `set_plan(on: bool) -> None`, `save() -> None`, `finish() -> None`
  - `RuntimeRegistry(build_agent, *, capacity=8)` with `.get(thread_id: str, workdir: str) -> SessionRuntime`
  - `WorkdirMismatch(Exception)`
  - `runtime_for(request, thread_id: str, workdir: str) -> SessionRuntime | JSONResponse`
  - `make_session_agent_factory(*, model=None) -> Callable[[SessionRuntime], object]`
  - `create_app(agent_factory=None, *, token, trust_check=None, session_agent_factory=None)`

- [ ] **Step 1: Write the failing tests** — `tests/test_server_runtime.py`:

```python
import pytest

from luna.core.session_state import SessionStateStore
from luna.server.runtime import RuntimeRegistry, WorkdirMismatch


def _registry(built, capacity=8):
    def build(runtime):
        built.append((runtime.thread_id, runtime.config().provider, runtime.config().model))
        return object()

    return RuntimeRegistry(build, capacity=capacity)


def test_each_session_gets_its_own_agent(tmp_path):
    built = []
    reg = _registry(built)
    a, b = reg.get("t1", str(tmp_path)), reg.get("t2", str(tmp_path))
    assert a.agent is not b.agent
    assert reg.get("t1", str(tmp_path)) is a


def test_switch_model_rebuilds_and_persists(tmp_path):
    built = []
    rt = _registry(built).get("t1", str(tmp_path))
    rt.agent  # noqa: B018 - first build
    rt.switch_model("gpt-5")
    assert built[-1][2] == "gpt-5"
    assert SessionStateStore().load("t1", str(tmp_path)).model == "gpt-5"


def test_failed_switch_restores_the_previous_model(tmp_path):
    calls = {"n": 0}

    def build(runtime):
        calls["n"] += 1
        if runtime.state.model == "bad":
            raise RuntimeError("unknown model")
        return object()

    rt = RuntimeRegistry(build).get("t1", str(tmp_path))
    rt.agent  # noqa: B018
    with pytest.raises(RuntimeError):
        rt.switch_model("bad")
    assert rt.state.model is None
    assert SessionStateStore().load("t1", str(tmp_path)).model is None


def test_switch_provider_resets_the_model(tmp_path):
    rt = _registry([]).get("t1", str(tmp_path))
    rt.switch_model("m1")
    rt.switch_provider("openai")
    assert (rt.state.provider, rt.state.model) == ("openai", None)
    assert rt.config().provider == "openai"


def test_plan_flag_is_per_session(tmp_path):
    reg = _registry([])
    one, two = reg.get("t1", str(tmp_path)), reg.get("t2", str(tmp_path))
    one.set_plan(True)
    assert one.state.plan is True and two.state.plan is False


def test_lru_evicts_idle_runtimes_and_reloads_state(tmp_path):
    reg = _registry([], capacity=1)
    first = reg.get("t1", str(tmp_path))
    first.set_plan(True)
    reg.get("t2", str(tmp_path))
    again = reg.get("t1", str(tmp_path))
    assert again is not first and again.state.plan is True


def test_same_thread_other_workdir_is_refused(tmp_path):
    reg = _registry([])
    reg.get("t1", str(tmp_path))
    with pytest.raises(WorkdirMismatch):
        reg.get("t1", str(tmp_path / "other"))
```

- [ ] **Step 2: Run** — Expected: FAIL, `ModuleNotFoundError: No module named 'luna.server.runtime'`.

- [ ] **Step 3: Implement `luna/server/runtime.py`**

```python
"""One runtime per conversation: its own agent, settings, lock and turn phase.

The server is shared by every project and every open session. An agent's
undo journal (``session_id``), plan-mode flag and edit-anchor tracker are
fixed when it is built, so each session gets its own agent (as Hermes does:
"a fresh AIAgent per session"), rebuilt only for that session on
``/model``, ``/provider`` or ``/reload``.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.config.config import LunaConfig, load_config
from luna.config.providers import LunaConfigError
from luna.core.session_state import SessionState, SessionStateStore
from luna.turn.engine import PreparedTurn, TurnOutcome


class WorkdirMismatch(Exception):
    """A session id was used with a different project folder than its own."""


class SessionRuntime:
    """Live server-side state of one session."""

    def __init__(
        self,
        state: SessionState,
        *,
        store: SessionStateStore,
        build_agent: Callable[[SessionRuntime], object],
    ) -> None:
        self.state = state
        self._store = store
        self._build_agent = build_agent
        self._agent: object | None = None
        self.phase = "idle"
        self.prepared: PreparedTurn | None = None
        self.outcome = TurnOutcome()
        self.lock = asyncio.Lock()
        #: Set by /init: rebuild after its turn so a new AGENTS.md is loaded.
        self.reload_after_turn = False

    @property
    def thread_id(self) -> str:
        return self.state.thread_id

    @property
    def workdir(self) -> str:
        return self.state.workdir

    @property
    def agent(self):
        """The session's agent, built on first use."""
        if self._agent is None:
            self._agent = self._build_agent(self)
        return self._agent

    def config(self) -> LunaConfig:
        """Project config with this session's provider/model applied on top."""
        workdir = self.workdir
        try:
            cfg = load_config({"workdir": workdir}, cwd=workdir)
        except (LunaConfigError, OSError):
            cfg = LunaConfig(workdir=workdir)
        if self.state.provider is not None:
            cfg.provider = self.state.provider
            cfg.model = self.state.model
        elif self.state.model is not None:
            cfg.model = self.state.model
        return cfg

    def rebuild(self) -> None:
        """Rebuild this session's agent (raises on a bad config)."""
        self._agent = self._build_agent(self)

    def _switch(self, provider: str | None, model: str | None) -> None:
        previous = (self.state.provider, self.state.model)
        self.state.provider, self.state.model = provider, model
        try:
            self.rebuild()
        except Exception:
            self.state.provider, self.state.model = previous
            raise
        self.save()

    def switch_model(self, model: str) -> None:
        """Use ``model`` for this session only; restores the old one on failure."""
        self._switch(self.state.provider, model)

    def switch_provider(self, provider: str) -> None:
        """Use ``provider`` (with its default model) for this session only."""
        self._switch(provider, None)

    def set_plan(self, on: bool) -> None:
        """Toggle plan mode for this session (the agent reads it per tool call)."""
        self.state.plan = on
        self.save()

    def save(self) -> None:
        self._store.save(self.state)

    def finish(self) -> None:
        """Return to idle after a turn (successful or not) and persist state."""
        self.phase = "idle"
        self.prepared = None
        self.outcome = TurnOutcome()
        self.save()


class RuntimeRegistry:
    """LRU of session runtimes; evicted ones are rebuilt from the state table."""

    def __init__(self, build_agent: Callable[[SessionRuntime], object], *, capacity: int = 8):
        self._build_agent = build_agent
        self._capacity = capacity
        self._items: OrderedDict[str, SessionRuntime] = OrderedDict()
        self._store: SessionStateStore | None = None

    def get(self, thread_id: str, workdir: str) -> SessionRuntime:
        resolved = str(Path(workdir).resolve())
        runtime = self._items.get(thread_id)
        if runtime is not None:
            if runtime.workdir != resolved:
                raise WorkdirMismatch(thread_id)
            self._items.move_to_end(thread_id)
            return runtime
        if self._store is None:
            self._store = SessionStateStore()
        state = self._store.load(thread_id, resolved)
        if state.workdir != resolved:
            raise WorkdirMismatch(thread_id)
        runtime = SessionRuntime(state, store=self._store, build_agent=self._build_agent)
        self._items[thread_id] = runtime
        for key in list(self._items):
            if len(self._items) <= self._capacity:
                break
            if not self._items[key].lock.locked() and key != thread_id:
                del self._items[key]
        return runtime


def runtime_for(request: Request, thread_id: str, workdir: str) -> SessionRuntime | JSONResponse:
    """The request's session runtime, or a 400 when the folder does not match."""
    try:
        return request.app.state.runtimes.get(thread_id, workdir)
    except WorkdirMismatch:
        return JSONResponse({"error": "workdir_mismatch"}, status_code=400)
```

- [ ] **Step 4: Add `make_session_agent_factory` in `luna/server/run.py`** (after `make_agent_factory`):

```python
def make_session_agent_factory(*, model=None) -> Callable[[object], object]:
    """Build one agent per *session* (not per workdir).

    Each agent gets the session's own undo journal (``session_id`` =
    thread id, as the REPL does), a plan-mode flag read from that session's
    state on every tool call, and the session's provider/model overrides.
    ``model`` injects a fake chat model in tests, like ``build_agent``'s.
    """

    def build(runtime) -> object:
        from luna.core.agent import build_agent
        from luna.core.persistence import checkpointer

        return build_agent(
            runtime.config(),
            model=model,
            checkpointer=checkpointer(),
            session_id=runtime.thread_id,
            plan_flag=lambda: runtime.state.plan,
        )

    return build
```

In `run_serve` pass `session_agent_factory=make_session_agent_factory()` to `create_app` (drop `agent_factory=make_agent_factory()`; keep `make_agent_factory` itself — `tests/test_server_agent_registry.py` uses it).

- [ ] **Step 5: Wire `create_app`** in `luna/server/app.py`:

```python
def create_app(
    agent_factory: Callable[[str], object] | None = None,
    *,
    token: str,
    trust_check: Callable[[str], bool] | None = None,
    session_agent_factory: Callable[[object], object] | None = None,
) -> Starlette:
```

Docstring addition: "``session_agent_factory(runtime)`` builds one agent per session (production). ``agent_factory(workdir)`` is the older per-workdir form kept for tests; it is adapted to a session factory." After building `app`:

```python
    if session_agent_factory is None:
        if agent_factory is None:
            raise TypeError("create_app needs agent_factory or session_agent_factory")
        session_agent_factory = lambda runtime: agent_factory(runtime.workdir)  # noqa: E731
    app.state.agent_factory = agent_factory
    app.state.runtimes = RuntimeRegistry(session_agent_factory)
```

(import `RuntimeRegistry` from `luna.server.runtime`).

- [ ] **Step 6: Run** `tests/test_server_runtime.py`, `tests/test_server_trust.py`, `tests/test_server_agent_registry.py`, full suite — Expected: all pass.

- [ ] **Step 7: Commit** — `git commit -m "feat: one runtime and agent per session on the server (own undo journal, plan flag, model)"`

---

### Task 6: Server turn flow on runtimes: engine, fix-up, notices, threadpool, 409

**Files:**
- Modify: `luna/server/turns.py` (replace `_stream_turn_events`; `post_message`, `get_history`), `luna/server/approvals.py`
- Test: `tests/test_server_turn_flow.py` (new); existing `tests/test_server_turns.py`, `tests/test_server_approvals.py` stay green (update only imports if they referenced `_stream_turn_events` in docstrings — no code does)

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces:
  - `luna.server.turns.stream_turn(runtime, payload, index) -> AsyncIterator[dict]`
  - SSE `{"event": "notice", "level": str, "text": str}`; `turn_done` once after the whole pipeline.

- [ ] **Step 1: Write the failing tests** — `tests/test_server_turn_flow.py`:

```python
import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.server.app import create_app
from luna.server.run import make_session_agent_factory


def _parse(raw: str) -> list[dict]:
    return [
        json.loads(line[5:].strip())
        for block in raw.strip().split("\n\n")
        for line in block.splitlines()
        if line.startswith("data:")
    ]


def _write_call(path="a.txt", call_id="w1"):
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": "write_file", "args": {"file_path": f"/{path}", "content": "x"}}],
    )


@pytest.fixture
def client_for(tmp_path):
    def make(*responses, verify=""):
        (tmp_path / ".luna.toml").write_text(
            f'[agent]\nverify_command = "{verify}"\nformat_command = ""\ndiagnose_command = ""\n'
        )
        from tests.conftest import FakeToolCallingModel

        model = FakeToolCallingModel(responses=list(responses))
        app = create_app(token="t", session_agent_factory=make_session_agent_factory(model=model))
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer t"},
        ), app

    return make


async def _say(c, tmp_path, text, thread="t1"):
    resp = await c.post(f"/sessions/{thread}/messages", json={"content": text, "workdir": str(tmp_path)})
    return resp, _parse(resp.text)


async def test_two_sessions_in_one_workdir_are_isolated(client_for, tmp_path):
    c, app = client_for(AIMessage(content="ok"))
    async with c:
        one = app.state.runtimes.get("t1", str(tmp_path))
        one.set_plan(True)
        two = app.state.runtimes.get("t2", str(tmp_path))
        assert two.state.plan is False
        assert one.agent is not two.agent


async def test_second_turn_while_streaming_is_409(client_for, tmp_path):
    c, app = client_for(AIMessage(content="ok"))
    async with c:
        runtime = app.state.runtimes.get("t1", str(tmp_path))
        await runtime.lock.acquire()
        try:
            resp, _ = await _say(c, tmp_path, "hi")
        finally:
            runtime.lock.release()
    assert resp.status_code == 409 and resp.json() == {"error": "session_busy"}


async def test_new_message_after_abandoned_approval_is_accepted(client_for, tmp_path):
    c, app = client_for(AIMessage(content="ok"))
    async with c:
        runtime = app.state.runtimes.get("t1", str(tmp_path))
        runtime.phase = "turn"  # paused on an approval nobody will answer
        resp, events = await _say(c, tmp_path, "hi")
    assert resp.status_code == 200 and events[-1] == {"event": "turn_done"}


async def test_mutating_turn_with_failing_verify_streams_notices_and_fixup(client_for, tmp_path, monkeypatch):
    from luna.turn import engine

    results = iter([(False, "boom"), (True, "")])
    monkeypatch.setattr(engine, "run_verify", lambda cmd, wd: next(results))
    c, _ = client_for(
        _write_call(),
        AIMessage(content="wrote it"),
        AIMessage(content="fixed it"),
        verify="make check",
    )
    async with c:
        _, events = await _say(c, tmp_path, "write a file")
    notices = [e for e in events if e["event"] == "notice"]
    assert {"event": "notice", "level": "warn", "text": "verify failed\nboom"} in notices
    assert {"event": "notice", "level": "ok", "text": "✓ verify ok"} in notices
    texts = "".join(e.get("text", "") for e in events if e["event"] == "text_delta")
    assert "fixed it" in texts
    assert [e["event"] for e in events].count("turn_done") == 1


async def test_approve_after_runtime_loss_still_finishes(client_for, tmp_path):
    c, app = client_for(_write_call(), AIMessage(content="done"))
    async with c:
        # make the write_file call require approval (no yolo, no allow rule)
        _, first = await _say(c, tmp_path, "write")
        assert first[-1]["event"] == "approval_needed"
        app.state.runtimes._items.clear()  # server restart / eviction
        resp = await c.post(
            "/sessions/t1/approve",
            json={"decision": {"type": "approve"}, "workdir": str(tmp_path)},
        )
    assert _parse(resp.text)[-1] == {"event": "turn_done"}


async def test_pinned_files_reach_the_model(client_for, tmp_path):
    (tmp_path / "keep.md").write_text("PINNED-CONTENT\n")
    c, app = client_for(AIMessage(content="ok"))
    async with c:
        runtime = app.state.runtimes.get("t1", str(tmp_path))
        runtime.state.pinned.add("keep.md")
        await _say(c, tmp_path, "hi")
        messages = runtime.agent.get_state({"configurable": {"thread_id": "t1"}}).values["messages"]
    assert "PINNED-CONTENT" in messages[0].content
```

(`[agent] verify_command / format_command / diagnose_command` is the schema `luna/config/config.py::_apply_toml` reads — verified. The approval-pause test relies on `write_file` being in `INTERRUPT_TOOLS` and the project having no allow rule; the fake model's first response is the write call.)

- [ ] **Step 2: Run** — Expected: FAIL (409 test gets 200; notice tests find no `notice` events; approve-after-loss raises).

- [ ] **Step 3: Rewrite the turn flow in `luna/server/turns.py`**

Imports to add: `from starlette.concurrency import iterate_in_threadpool, run_in_threadpool`, `from luna.core.turn_events import ReloadRequested, ToolFinished, UsageDelta` (extend the existing import), `from luna.extensions.subagents import subagent_summaries`, `from luna.config.providers import LunaConfigError`, `from luna.server.runtime import runtime_for`, `from luna.turn import engine`.

Replace `_stream_turn_events` with:

```python
def _rule_decision(interrupt_value: dict, rules) -> dict | None:
    """Auto-answer an interrupt from project permission rules, else ``None``."""
    requests = interrupt_value.get("action_requests") or [interrupt_value.get("action_request")]
    request = requests[0] or {}
    name = request.get("action") or request.get("name")
    args = request.get("args", {}) or {}
    verdict = rules.match(name, args)
    if verdict == "allow":
        return {"type": "approve"}
    if verdict == "deny":
        return {"type": "reject", "message": f"blocked by a Luna permission rule ({name})"}
    return None


def _graph_events(agent, payload, config: dict, rules, outcome: engine.TurnOutcome):
    """Run the graph (sync; called in a worker thread) and yield SSE dicts.

    Yields ``("interrupt", value)`` last when the turn pauses for a human
    decision that no permission rule answers. Usage, tool names and reload
    requests are folded into ``outcome`` for :func:`engine.finish_turn`.
    """
    while True:
        interrupt_value = None
        for event in iter_turn(agent, payload, config):
            if isinstance(event, Interrupted):
                interrupt_value = event.value
                break
            if isinstance(event, UsageDelta):
                outcome.usage.merge(event.usage_metadata)
            elif isinstance(event, ToolFinished) and event.name:
                outcome.tool_names.add(event.name)
            elif isinstance(event, ReloadRequested):
                outcome.reload_requested = True
            yield _event_dict(event)
        if interrupt_value is None:
            return
        decision = _rule_decision(interrupt_value, rules)
        if decision is None:
            yield ("interrupt", interrupt_value)
            return
        payload = Command(resume={"decisions": [decision]})


def _sse(obj: dict) -> dict:
    return {"data": json.dumps(obj)}


def _notice(notice: engine.Notice) -> dict:
    return _sse({"event": "notice", "level": notice.level, "text": notice.text})


async def stream_turn(runtime, payload, index):
    """Stream a turn (or a resumed one) through the whole REPL-equivalent pipeline.

    Holds the session lock while streaming, so a second turn gets 409. The
    graph runs in a worker thread, keeping the event loop free for read-only
    commands mid-turn. When the graph finishes without a pending approval:
    ``finish_turn`` (usage, index, format/diagnose, verify), auto-reload, and
    at most one fix-up turn streamed in this same response, then
    ``finish_fixup``. ``turn_done`` is sent once, at the very end.
    """
    config = {"configurable": {"thread_id": runtime.thread_id}}
    rules = permissions.load_rules(runtime.workdir)
    async with runtime.lock:
        try:
            while True:
                paused = None
                events = _graph_events(runtime.agent, payload, config, rules, runtime.outcome)
                async for item in iterate_in_threadpool(events):
                    if isinstance(item, tuple):
                        paused = item[1]
                        break
                    yield _sse(item)
                if paused is not None:
                    yield _sse({"event": "approval_needed", "value": paused})
                    return
                cfg = await run_in_threadpool(runtime.config)
                if runtime.phase == "fixup":
                    for notice in await run_in_threadpool(engine.finish_fixup, cfg):
                        yield _notice(notice)
                else:
                    prepared = runtime.prepared or engine.PreparedTurn("", "", [])
                    result = await run_in_threadpool(
                        lambda: engine.finish_turn(
                            runtime.state,
                            prepared,
                            runtime.outcome,
                            cfg=cfg,
                            index=index,
                            thread_id=runtime.thread_id,
                            workdir=runtime.workdir,
                        )
                    )
                    for notice in result.notices:
                        yield _notice(notice)
                    if result.reload or runtime.reload_after_turn:
                        runtime.reload_after_turn = False
                        try:
                            await run_in_threadpool(runtime.rebuild)
                            yield _notice(
                                engine.Notice("info", "auto-reloaded — new capabilities are live")
                            )
                        except Exception as exc:  # noqa: BLE001 - a bad config must not end the turn
                            yield _notice(engine.Notice("error", f"auto-reload failed: {exc}"))
                    if result.fixup_prompt is not None:
                        runtime.phase = "fixup"
                        runtime.outcome = engine.TurnOutcome()
                        payload = {"messages": [{"role": "user", "content": result.fixup_prompt}]}
                        continue
                runtime.finish()
                yield _sse({"event": "turn_done"})
                return
        except Exception as exc:
            logging.getLogger(__name__).exception("turn failed for thread %s", runtime.thread_id)
            runtime.finish()
            yield _sse({"event": "error", "message": f"{type(exc).__name__}: {exc}"})
```

`post_message`:

```python
async def post_message(request: Request) -> EventSourceResponse | JSONResponse:
    """Prepare a turn like the REPL does and stream it as SSE."""
    thread_id = request.path_params["thread_id"]
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    content = body["content"]
    workdir = body.get("workdir", ".")
    runtime = runtime_for(request, thread_id, workdir)
    if isinstance(runtime, JSONResponse):
        return runtime
    if runtime.lock.locked():
        return JSONResponse({"error": "session_busy"}, status_code=409)

    def _prepare():
        try:
            names = {n for n, _ in subagent_summaries(runtime.workdir)}
        except LunaConfigError:
            names = set()
        return engine.prepare_turn(
            runtime.state,
            content,
            agent=runtime.agent,
            thread_id=thread_id,
            workdir=runtime.workdir,
            session_id=thread_id,
            subagent_names=names,
        )

    runtime.prepared = await run_in_threadpool(_prepare)
    runtime.phase = "turn"
    runtime.outcome = engine.TurnOutcome()
    index = SessionIndex()
    existing = {r.thread_id for r in index.list(workdir=runtime.workdir)}
    if thread_id not in existing:
        index.record(thread_id, runtime.workdir, make_title(content))
    index.touch(thread_id)
    payload = {"messages": [{"role": "user", "content": runtime.prepared.content}]}
    return EventSourceResponse(stream_turn(runtime, payload, index))
```

`get_history`: replace `agent = request.app.state.agent_factory(workdir)` with the runtime (`runtime_for` → on JSONResponse return it) and `runtime.agent`.

`luna/server/approvals.py::post_approve`:

```python
    thread_id = request.path_params["thread_id"]
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    workdir = body.get("workdir", ".")
    runtime = runtime_for(request, thread_id, workdir)
    if isinstance(runtime, JSONResponse):
        return runtime
    if runtime.lock.locked():
        return JSONResponse({"error": "session_busy"}, status_code=409)
    decision = dict(body["decision"])
    if "always" in decision:
        permissions.append_project_rule(runtime.workdir, decision["always"])
        decision.pop("always")
    if runtime.phase == "idle":
        runtime.phase = "turn"  # resumed after a restart/eviction: finish as a normal turn
    index = SessionIndex()
    index.touch(thread_id)
    payload = Command(resume={"decisions": [decision]})
    return EventSourceResponse(stream_turn(runtime, payload, index))
```

(imports: `SessionIndex`, `runtime_for`, `stream_turn` from turns.)

- [ ] **Step 4: Run** the new file, `tests/test_server_turns.py`, `tests/test_server_approvals.py`, `tests/test_tui_*.py`, full suite — Expected: all pass. If an existing test asserted `turn_done` directly after an approval with no pipeline, it still holds (a read-only turn emits no notices).

- [ ] **Step 5: Commit** — `git commit -m "feat: server turns run the REPL pipeline per session (undo, pinned, @mentions, verify + fix-up, reload) off the event loop"`

---

### Task 7: Command registry core + read commands

**Files:**
- Create: `luna/commands/__init__.py`, `luna/commands/base.py`, `luna/commands/registry.py`, `luna/commands/builtin.py`
- Test: `tests/test_command_registry.py`

**Interfaces:**
- Consumes: `Notice` (Task 2); `SessionUsage`; `PinnedFiles`; `session_diff`; `subagent_summaries`; `usercmd.expand`.
- Produces:
  - `luna/commands/base.py`: `Choice(title: str, options: list[tuple[str, str]], resubmit: str)`, `Confirm(question: str, resubmit: str, cancelled: str)`, `CommandResult(text="", notices=[], choice=None, confirm=None, prompt=None, effects={})` with `.to_json() -> dict`, `Command(name, help, kind, run)`, `CommandEnv` Protocol (below), `KINDS`.
  - `luna/commands/registry.py`: `REGISTRY: dict[str, Command]`, `register(name, help, kind)`, `run_line(line: str, env) -> CommandResult`, `list_commands(user_commands: dict) -> list[dict]`, `command_kind(name: str, user_commands: dict) -> str`.
  - `luna/commands/__init__.py` re-exports the above and imports `builtin` so every command is registered.

`CommandEnv` protocol (implemented by the REPL adapter in Task 10 and the server in Task 11):

```python
class CommandEnv(Protocol):
    workdir: str
    thread_id: str
    session_id: str
    index: object | None
    user_commands: dict
    pinned: PinnedFiles
    usage: SessionUsage

    @property
    def config(self) -> LunaConfig: ...
    @property
    def agent(self) -> object: ...
    @property
    def can_rebuild(self) -> bool: ...
    def get_plan(self) -> bool | None: ...  # None = plan mode not available here
    def set_plan(self, on: bool) -> None: ...
    def rebuild(self) -> None: ...          # raises on failure
    def switch_model(self, model: str) -> None: ...      # raises; restores on failure
    def switch_provider(self, provider: str) -> None: ...  # raises; restores on failure
    def after_turn_reload(self) -> None: ...  # /init: rebuild after the prompt's turn
```

- [ ] **Step 1: Write the failing tests** — `tests/test_command_registry.py` (a `FakeEnv` reused by Tasks 8–9):

```python
from dataclasses import dataclass, field

from luna.commands import REGISTRY, CommandResult, list_commands, run_line
from luna.config.config import LunaConfig
from luna.config.usage import SessionUsage, TurnUsage
from luna.turn.context import PinnedFiles
from luna.turn.engine import Notice


@dataclass
class FakeEnv:
    workdir: str = "."
    thread_id: str = "t1"
    session_id: str = "t1"
    index: object = None
    user_commands: dict = field(default_factory=dict)
    pinned: PinnedFiles = field(default_factory=PinnedFiles)
    usage: SessionUsage = field(default_factory=SessionUsage)
    cfg: LunaConfig = field(default_factory=LunaConfig)
    plan: bool | None = False
    rebuilt: int = 0
    fail_rebuild: Exception | None = None
    reload_after: bool = False

    @property
    def config(self):
        return self.cfg

    @property
    def agent(self):
        return object()

    @property
    def can_rebuild(self):
        return True

    def get_plan(self):
        return self.plan

    def set_plan(self, on):
        self.plan = on

    def rebuild(self):
        if self.fail_rebuild:
            raise self.fail_rebuild
        self.rebuilt += 1

    def switch_model(self, model):
        previous = self.cfg.model
        self.cfg.model = model
        try:
            self.rebuild()
        except Exception:
            self.cfg.model = previous
            raise

    def switch_provider(self, provider):
        previous = (self.cfg.provider, self.cfg.model)
        self.cfg.provider, self.cfg.model = provider, None
        try:
            self.rebuild()
        except Exception:
            self.cfg.provider, self.cfg.model = previous
            raise

    def after_turn_reload(self):
        self.reload_after = True


def test_all_24_repl_commands_are_registered():
    from luna.repl.commands import HELP

    assert set(HELP) <= set(REGISTRY) | {"/exit"}
    assert {c["kind"] for c in list_commands({})} == {"ui", "read", "mutate", "prompt"}


def test_tools_lists_builtin_tools():
    result = run_line("/tools", FakeEnv())
    assert "read_file" in result.text and "web_search" in result.text


def test_usage_without_turns_and_with_turns():
    env = FakeEnv()
    assert run_line("/usage", env).notices == [Notice("info", "no usage recorded yet")]
    env.usage.add_turn(TurnUsage(10, 5, 15))
    assert "turns: 1  in: 10  out: 5  total: 15" in run_line("/usage", env).text


def test_context_lists_pinned_files():
    env = FakeEnv()
    assert run_line("/context", env).notices == [Notice("dim", "(no pinned files)")]
    env.pinned.add("a.py")
    assert run_line("/context", env).text == "a.py"


def test_diff_without_changes(tmp_path):
    assert run_line("/diff", FakeEnv(workdir=str(tmp_path))).notices == [
        Notice("dim", "no changes this session")
    ]


def test_user_command_expands_to_a_prompt(tmp_path):
    from luna.repl.usercmd import UserCommand

    env = FakeEnv(workdir=str(tmp_path), user_commands={"greet": UserCommand("greet", "say hi", "hi $ARGUMENTS")})
    assert run_line("/greet world", env) == CommandResult(prompt="hi world")
    assert {"name": "/greet", "help": "say hi", "kind": "prompt"} in list_commands(env.user_commands)


def test_unknown_command_is_an_error_notice():
    assert run_line("/nope", FakeEnv()).notices == [
        Notice("error", "неизвестная команда /nope — /help")
    ]


def test_result_serialises_for_the_api():
    from luna.commands import Choice

    result = CommandResult(text="x", notices=[Notice("info", "y")], choice=Choice("T", [("a", "A")], "/model {value}"))
    assert result.to_json() == {
        "text": "x",
        "notices": [{"level": "info", "text": "y"}],
        "choice": {"title": "T", "options": [["a", "A"]], "resubmit": "/model {value}"},
        "confirm": None,
        "prompt": None,
        "effects": {},
    }
```

- [ ] **Step 2: Run** — Expected: FAIL, `ModuleNotFoundError: No module named 'luna.commands'`.

- [ ] **Step 3: Implement `luna/commands/base.py`**

```python
"""Transport-neutral slash-command types.

A handler never prints and never reads input: it returns a
:class:`CommandResult`. The REPL renders it to a console; the server
returns it as JSON and the TUI renders it as widgets. A question for the
user (a picker or a yes/no) is a ``choice`` / ``confirm`` whose answer is
simply the same command re-sent with an argument, so no surface has to
keep a pending question.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Protocol

from luna.config.config import LunaConfig
from luna.config.usage import SessionUsage
from luna.turn.context import PinnedFiles
from luna.turn.engine import Notice

#: ui = handled by the client; read = allowed mid-turn; mutate = changes the
#: session; prompt = becomes a message to the agent.
KINDS = ("ui", "read", "mutate", "prompt")


@dataclass(frozen=True)
class Choice:
    """Ask the user to pick one option, then re-send ``resubmit.format(value=...)``."""

    title: str
    options: list[tuple[str, str]]
    resubmit: str


@dataclass(frozen=True)
class Confirm:
    """Ask yes/no; on yes re-send ``resubmit``, on no show ``cancelled``."""

    question: str
    resubmit: str
    cancelled: str


@dataclass
class CommandResult:
    """Everything a command wants a client to show or do."""

    text: str = ""
    notices: list[Notice] = field(default_factory=list)
    choice: Choice | None = None
    confirm: Confirm | None = None
    prompt: str | None = None
    effects: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        """JSON shape of ``POST /sessions/{id}/command``."""
        return {
            "text": self.text,
            "notices": [asdict(n) for n in self.notices],
            "choice": (
                {
                    "title": self.choice.title,
                    "options": [list(o) for o in self.choice.options],
                    "resubmit": self.choice.resubmit,
                }
                if self.choice
                else None
            ),
            "confirm": asdict(self.confirm) if self.confirm else None,
            "prompt": self.prompt,
            "effects": self.effects,
        }


class CommandEnv(Protocol):
    """What a handler may use; see the REPL and server adapters."""

    workdir: str
    thread_id: str
    session_id: str
    index: object | None
    user_commands: dict
    pinned: PinnedFiles
    usage: SessionUsage

    @property
    def config(self) -> LunaConfig: ...

    @property
    def agent(self) -> object: ...

    @property
    def can_rebuild(self) -> bool: ...

    def get_plan(self) -> bool | None: ...

    def set_plan(self, on: bool) -> None: ...

    def rebuild(self) -> None: ...

    def switch_model(self, model: str) -> None: ...

    def switch_provider(self, provider: str) -> None: ...

    def after_turn_reload(self) -> None: ...


@dataclass(frozen=True)
class Command:
    """One registered slash command."""

    name: str
    help: str
    kind: str
    run: Callable[[CommandEnv, str], CommandResult] | None
```

- [ ] **Step 4: Implement `luna/commands/registry.py`**

```python
"""The single slash-command registry shared by the REPL, the server and the TUI."""

from __future__ import annotations

from luna.commands.base import Command, CommandResult
from luna.turn.engine import Notice

REGISTRY: dict[str, Command] = {}


def register(name: str, help: str, kind: str):
    """Decorator registering ``fn(env, arg) -> CommandResult`` as ``name``."""

    def deco(fn):
        REGISTRY[name] = Command(name, help, kind, fn)
        return fn

    return deco


def register_ui(name: str, help: str) -> None:
    """A client-side command: listed for help/autocomplete, never run here."""
    REGISTRY[name] = Command(name, help, "ui", None)


def command_kind(name: str, user_commands: dict) -> str:
    """Kind of ``name``; user commands are prompts; unknown counts as mutate."""
    if name in REGISTRY:
        return REGISTRY[name].kind
    return "prompt" if name[1:] in user_commands else "mutate"


def run_line(line: str, env) -> CommandResult:
    """Run one ``/command args`` line against ``env``."""
    name, _, arg = line.strip().partition(" ")
    arg = arg.strip()
    command = REGISTRY.get(name)
    if command is not None and command.run is not None:
        return command.run(env, arg)
    bare = name[1:]
    if command is None and bare in (env.user_commands or {}):
        from luna.repl.usercmd import expand

        return CommandResult(prompt=expand(env.user_commands[bare], arg, env.workdir))
    return CommandResult(notices=[Notice("error", f"неизвестная команда {name} — /help")])


def list_commands(user_commands: dict) -> list[dict]:
    """Built-in + user commands as ``[{name, help, kind}]`` for help/autocomplete."""
    out = [{"name": c.name, "help": c.help, "kind": c.kind} for c in REGISTRY.values()]
    for name, cmd in sorted((user_commands or {}).items()):
        if f"/{name}" not in REGISTRY:
            out.append({"name": f"/{name}", "help": cmd.description, "kind": "prompt"})
    return out
```

- [ ] **Step 5: Implement `luna/commands/builtin.py` — registration of all 24 in `HELP` order, with the read commands implemented** (mutate/prompt bodies land in Tasks 8–9; register them now with `kind` and a stub body `lambda env, arg: CommandResult(notices=[Notice("error", "not implemented")])`, replaced in those tasks):

```python
"""Built-in slash commands. Strings match the REPL's historical output."""

from __future__ import annotations

from luna.commands.base import CommandResult
from luna.commands.registry import register, register_ui
from luna.config.providers import LunaConfigError
from luna.turn.engine import Notice

TOOL_NAMES = (
    "ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep",
    "execute", "write_todos", "task", "manage_mcp", "manage_skills",
    "save_skill", "web_search",
)

register_ui("/help", "show this help")


@register("/tools", "list the agent's tools", "read")
def _tools(env, arg):
    return CommandResult(
        text=", ".join(TOOL_NAMES) + "\n\n*(+ any MCP tools as mcp__<server>__<tool>)*"
    )


@register("/agents", "list available subagents", "read")
def _agents(env, arg):
    from luna.extensions.subagents import subagent_summaries

    try:
        summaries = subagent_summaries(env.workdir)
    except LunaConfigError as exc:
        return CommandResult(notices=[Notice("error", str(exc))])
    if not summaries:
        return CommandResult(notices=[Notice("dim", "(no subagents)")])
    return CommandResult(text="\n".join(f"- **{n}** — {d}" for n, d in summaries))


register_ui("/sessions", "list past sessions for this directory")
register_ui("/resume", "resume a past session")


@register("/usage", "show token usage this session", "read")
def _usage(env, arg):
    if not env.usage.turns:
        return CommandResult(notices=[Notice("info", "no usage recorded yet")])
    in_tok, out_tok, total = env.usage.totals
    lines = [f"turns: {len(env.usage.turns)}  in: {in_tok}  out: {out_tok}  total: {total}"]
    cost = env.usage.cost(env.config.provider, env.config.model, env.config.pricing)
    if cost is not None:
        lines.append(f"cost: ${cost:.4f}")
    return CommandResult(text="\n".join(lines))


# /compact registered in Task 9


@register("/diff", "show file changes made this session", "read")
def _diff(env, arg):
    from luna.turn.undo import session_diff

    text = session_diff(env.workdir, env.session_id)
    if not text:
        return CommandResult(notices=[Notice("dim", "no changes this session")])
    return CommandResult(text=f"```diff\n{text}\n```")


# /undo /redo /add /drop registered in Tasks 8–9


@register("/context", "list pinned files", "read")
def _context(env, arg):
    if not env.pinned.paths:
        return CommandResult(notices=[Notice("dim", "(no pinned files)")])
    return CommandResult(text="\n".join(env.pinned.paths))


@register("/commands", "list custom slash commands", "read")
def _commands(env, arg):
    cmds = env.user_commands or {}
    if not cmds:
        return CommandResult(notices=[Notice("dim", "(no custom commands)")])
    return CommandResult(
        text="\n".join(f"- `/{n}` — {c.description}" for n, c in sorted(cmds.items()))
    )


register_ui("/new", "start a fresh conversation thread")
register_ui("/clear", "clear the screen")
```

Registration order in `REGISTRY` must follow `luna/repl/commands.py::HELP` order so `/help` output stays ordered; place the Task 8–9 registrations at the matching positions (the stubs keep the order). `/exit` stays handled by each client (not registered). `luna/commands/__init__.py`:

```python
"""Slash commands shared by every Luna surface (REPL, server, TUI)."""

from luna.commands import builtin  # noqa: F401 - registers the built-ins
from luna.commands.base import Choice, Command, CommandEnv, CommandResult, Confirm
from luna.commands.registry import REGISTRY, command_kind, list_commands, run_line

__all__ = [
    "REGISTRY", "Choice", "Command", "CommandEnv", "CommandResult", "Confirm",
    "command_kind", "list_commands", "run_line",
]
```

- [ ] **Step 6: Run** the new tests (the two tests touching Task 8–9 commands are not in this file) and the full suite — Expected: all pass.

- [ ] **Step 7: Commit** — `git commit -m "feat: shared slash-command registry with structured results; read-only commands"`

---

### Task 8: Mutating commands: plan, add/drop, verify, diagnose, reload, model, provider

**Files:**
- Modify: `luna/commands/builtin.py`
- Test: `tests/test_command_registry.py` (append)

**Interfaces:**
- Consumes: Task 7; `luna.config.model_discovery.list_models/known_models`; `merge_providers`; `get_api_key`; `run_verify`; `diagnose`.

- [ ] **Step 1: Write the failing tests** — append:

```python
from luna.commands import Choice


def test_plan_toggles_and_accepts_on_off():
    env = FakeEnv()
    assert run_line("/plan", env).notices == [Notice("info", "plan mode: on")]
    assert env.plan is True
    assert run_line("/plan off", env).effects == {"plan": False}
    assert run_line("/plan", FakeEnv(plan=None)).notices == [Notice("dim", "plan mode is not available here")]


def test_add_and_drop_pin_files():
    env = FakeEnv()
    assert run_line("/add", env).notices == [Notice("dim", "usage: /add path ...")]
    assert run_line("/add a.py b.py", env).notices == [Notice("dim", "pinned: a.py, b.py")]
    assert run_line("/drop a.py b.py", env).notices == [Notice("dim", "pinned: (none)")]


def test_verify_without_command_and_with_failure(monkeypatch):
    from luna.commands import builtin

    assert run_line("/verify", FakeEnv()).notices == [Notice("dim", "set agent.verify_command in config first")]
    monkeypatch.setattr(builtin, "run_verify", lambda cmd, wd: (False, "E"))
    env = FakeEnv(cfg=LunaConfig(verify_command="make check"))
    assert run_line("/verify", env).notices == [Notice("warn", "verify failed\nE")]


def test_reload_rebuilds_and_reports_failure():
    env = FakeEnv()
    assert run_line("/reload", env).effects == {"agent_rebuilt": True} and env.rebuilt == 1
    bad = FakeEnv(fail_rebuild=RuntimeError("bad toml"))
    assert run_line("/reload", bad).notices == [Notice("error", "/reload failed: bad toml")]


def test_model_with_arg_switches_and_failure_keeps_the_old_one():
    env = FakeEnv()
    result = run_line("/model gpt-5", env)
    assert result.notices == [Notice("info", "model → gpt-5")] and result.effects == {"model": "gpt-5"}
    bad = FakeEnv(fail_rebuild=RuntimeError("no such model"))
    assert run_line("/model nope", bad).notices == [Notice("error", "could not switch: no such model")]
    assert bad.cfg.model is None


def test_model_without_arg_offers_a_choice(monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_models_for", lambda cfg: ["a", "b"])
    result = run_line("/model", FakeEnv())
    assert result.choice == Choice("Модель (anthropic)", [("a", "a"), ("b", "b")], "/model {value}")
    assert result.notices == [Notice("dim", "model: (provider default)")]


def test_model_without_arg_and_no_list_explains_usage(monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_models_for", lambda cfg: [])
    result = run_line("/model", FakeEnv())
    assert result.choice is None
    assert result.notices == [Notice("dim", "model: (provider default)")]


def test_provider_unknown_and_missing_key(monkeypatch):
    assert run_line("/provider nope", FakeEnv()).notices == [Notice("error", "unknown provider 'nope'")]
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    notices = run_line("/provider openai", FakeEnv()).notices
    assert notices == [Notice("error", "no key for openai; run: luna config set-key openai")]
```

(If the default provider in `LunaConfig` is not `anthropic`, adjust the expected choice title in `test_model_without_arg_offers_a_choice` to `f"Модель ({LunaConfig().provider})"` — write it that way from the start.)

- [ ] **Step 2: Run** — Expected: FAIL on every new test (stub bodies).

- [ ] **Step 3: Implement in `luna/commands/builtin.py`** (replace the stubs; add imports `import os`, `from luna.commands.base import Choice`, `from luna.config.credentials import get_api_key`, `from luna.config.providers import merge_providers`, `from luna.turn.verify import run_verify`):

```python
def _api_key(provider: str, spec) -> str | None:
    key = os.environ.get(spec.env_var) if spec.env_var else None
    return key or (get_api_key(provider) if spec.env_var else None)


def _models_for(cfg) -> list[str]:
    """Live model ids for the session's provider, else the bundled list."""
    from luna.config import model_discovery

    spec = merge_providers(cfg.custom_providers)[cfg.provider]
    models = model_discovery.list_models(cfg.provider, spec, api_key=_api_key(cfg.provider, spec))
    return models if models is not None else model_discovery.known_models(cfg.provider)


@register("/model", "pick a model interactively, or /model <name> to switch directly", "mutate")
def _model(env, arg):
    if not arg:
        current = Notice("dim", f"model: {env.config.model or '(provider default)'}")
        models = _models_for(env.config)
        if not models:
            return CommandResult(notices=[current])
        return CommandResult(
            notices=[current],
            choice=Choice(
                f"Модель ({env.config.provider})", [(m, m) for m in models], "/model {value}"
            ),
        )
    try:
        env.switch_model(arg)
    except Exception as exc:  # noqa: BLE001 - a bad model must not kill the session
        return CommandResult(notices=[Notice("error", f"could not switch: {exc}")])
    return CommandResult(notices=[Notice("info", f"model → {arg}")], effects={"model": arg})


@register("/provider", "show or switch the provider (/provider <key>)", "mutate")
def _provider(env, arg):
    registry = merge_providers(env.config.custom_providers)
    if not arg:
        current = Notice("dim", f"provider: {env.config.provider}")
        usable = [k for k, spec in registry.items() if not spec.env_var or _api_key(k, spec)]
        return CommandResult(
            notices=[current],
            choice=Choice("Провайдер", [(k, k) for k in usable], "/provider {value}"),
        )
    if arg not in registry:
        return CommandResult(notices=[Notice("error", f"unknown provider {arg!r}")])
    spec = registry[arg]
    if spec.env_var and not _api_key(arg, spec):
        return CommandResult(
            notices=[Notice("error", f"no key for {arg}; run: luna config set-key {arg}")]
        )
    try:
        env.switch_provider(arg)
    except Exception as exc:  # noqa: BLE001 - a bad provider must not kill the session
        return CommandResult(notices=[Notice("error", f"could not switch: {exc}")])
    return CommandResult(notices=[Notice("info", f"provider → {arg}")], effects={"provider": arg})


@register("/reload", "rebuild the agent with the current config", "mutate")
def _reload(env, arg):
    if not env.can_rebuild:
        return CommandResult(notices=[Notice("error", "/reload is not available here")])
    try:
        env.rebuild()
    except Exception as exc:  # noqa: BLE001 - a bad config must not kill the session
        return CommandResult(notices=[Notice("error", f"/reload failed: {exc}")])
    return CommandResult(
        notices=[Notice("info", "reloaded — capabilities refreshed")],
        effects={"agent_rebuilt": True},
    )


@register("/add", "pin files into context (/add path ...)", "mutate")
def _add(env, arg):
    if not arg:
        return CommandResult(notices=[Notice("dim", "usage: /add path ...")])
    env.pinned.add(*arg.split())
    return CommandResult(
        notices=[Notice("dim", f"pinned: {', '.join(env.pinned.paths)}")],
        effects={"pinned": len(env.pinned.paths)},
    )


@register("/drop", "unpin files (/drop path ...)", "mutate")
def _drop(env, arg):
    env.pinned.drop(*arg.split())
    return CommandResult(
        notices=[Notice("dim", f"pinned: {', '.join(env.pinned.paths) or '(none)'}")],
        effects={"pinned": len(env.pinned.paths)},
    )


@register("/verify", "run the project's verify command now", "mutate")
def _verify(env, arg):
    if not env.config.verify_command:
        return CommandResult(notices=[Notice("dim", "set agent.verify_command in config first")])
    ok, tail = run_verify(env.config.verify_command, env.workdir)
    return CommandResult(
        notices=[Notice("ok", "✓ verify ok") if ok else Notice("warn", f"verify failed\n{tail}")]
    )


@register("/diagnose", "run the project's diagnostics command now", "mutate")
def _diagnose(env, arg):
    from luna.turn import diagnose

    cmd = env.config.diagnose_command
    if cmd == "auto":
        cmd = diagnose.detect(env.workdir)
    if not cmd:
        return CommandResult(notices=[Notice("dim", "no diagnose command configured or detected")])
    text = diagnose.run(cmd, env.workdir, [])
    return CommandResult(text=text) if text else CommandResult(notices=[Notice("dim", "no findings")])


@register("/plan", "toggle plan mode (blocks writes/execute)", "mutate")
def _plan(env, arg):
    current = env.get_plan()
    if current is None:
        return CommandResult(notices=[Notice("dim", "plan mode is not available here")])
    on = arg == "on" if arg in ("on", "off") else not current
    env.set_plan(on)
    return CommandResult(
        notices=[Notice("info", f"plan mode: {'on' if on else 'off'}")], effects={"plan": on}
    )
```

- [ ] **Step 4: Run** new tests + full suite — Expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "feat: registry commands /plan /add /drop /verify /diagnose /reload /model /provider"`

---

### Task 9: `/undo`, `/redo`, `/compact`, `/init`

**Files:**
- Modify: `luna/commands/builtin.py`
- Test: `tests/test_command_registry.py` (append)

**Interfaces:**
- Consumes: Task 7–8; `luna.turn.undo.peek_last/undo_last/undo/redo/forget_messages`; `luna.core.session.compact_history` (Task 3); `luna.extensions.initgen.init_prompt`.

- [ ] **Step 1: Write the failing tests** — append:

```python
from luna.commands import Confirm


def test_undo_asks_first_then_undoes(tmp_path, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_is_git", lambda wd: False)
    monkeypatch.setattr(builtin, "peek_last", lambda wd, sid: "undo write_file a.py")
    monkeypatch.setattr(builtin, "undo_last", lambda wd, sid: "reverted a.py")
    env = FakeEnv(workdir=str(tmp_path))
    assert run_line("/undo", env).confirm == Confirm("undo write_file a.py?", "/undo --yes", "undo cancelled")
    assert run_line("/undo --yes", env).notices == [Notice("info", "reverted a.py")]


def test_undo_with_nothing_to_undo(tmp_path, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_is_git", lambda wd: False)
    monkeypatch.setattr(builtin, "peek_last", lambda wd, sid: None)
    assert run_line("/undo", FakeEnv(workdir=str(tmp_path))).notices == [Notice("dim", "nothing to undo")]


def test_redo_needs_git(tmp_path, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_is_git", lambda wd: False)
    assert run_line("/redo", FakeEnv(workdir=str(tmp_path))).notices == [Notice("dim", "redo needs a git repository")]


def test_compact_reports_and_resyncs_undo(monkeypatch):
    from luna.commands import builtin

    forgot = []
    monkeypatch.setattr(builtin, "compact_history", lambda agent, tid: (True, "compacted — history replaced with a summary"))
    monkeypatch.setattr(builtin, "_message_count", lambda env: 1)
    monkeypatch.setattr(builtin, "forget_messages", lambda wd, sid, n: forgot.append(n))
    assert run_line("/compact", FakeEnv()).notices == [Notice("info", "compacted — history replaced with a summary")]
    assert forgot == [1]


def test_init_becomes_a_prompt_and_reloads_after(tmp_path, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "init_prompt", lambda wd: "WRITE AGENTS.md")
    env = FakeEnv(workdir=str(tmp_path))
    assert run_line("/init", env).prompt == "WRITE AGENTS.md"
    assert env.reload_after is True
```

- [ ] **Step 2: Run** — Expected: FAIL.

- [ ] **Step 3: Implement** (module-level imports so tests can monkeypatch: `from luna.core.session import compact_history` — **lazy import inside a helper would defeat monkeypatching, so import at module level only if it does not create a cycle**: `luna.core.session` imports `luna.repl.commands`, which (after Task 10) imports `luna.commands`; to avoid the cycle, define thin module-level wrappers that import lazily and patch those):

```python
def _is_git(workdir: str) -> bool:
    from luna.turn import gitinfo

    return gitinfo.is_git_repo(workdir)


def peek_last(workdir, session_id):
    from luna.turn.undo import peek_last as _peek

    return _peek(workdir, session_id)


def undo_last(workdir, session_id):
    from luna.turn.undo import undo_last as _undo

    return _undo(workdir, session_id)


def forget_messages(workdir, session_id, count):
    from luna.turn.undo import forget_messages as _forget

    return _forget(workdir, session_id, count)


def compact_history(agent, thread_id):
    from luna.core.session import compact_history as _compact

    return _compact(agent, thread_id)


def init_prompt(workdir):
    from luna.extensions.initgen import init_prompt as _init

    return _init(workdir)


def _message_count(env) -> int:
    try:
        config = {"configurable": {"thread_id": env.thread_id}}
        return len(env.agent.get_state(config).values.get("messages", []))
    except Exception:  # noqa: BLE001 - best-effort ledger resync
        return 0


@register("/undo", "revert the last file change", "mutate")
def _undo(env, arg):
    yes = arg.strip() == "--yes"
    try:
        if _is_git(env.workdir):
            if not yes:
                return CommandResult(
                    confirm=Confirm(
                        "undo the last turn (files + conversation)?", "/undo --yes", "undo cancelled"
                    )
                )
            from luna.turn.undo import undo as git_undo

            note = git_undo(env.workdir, env.session_id, env.agent, env.thread_id)
        else:
            desc = peek_last(env.workdir, env.session_id)
            if desc is None:
                return CommandResult(notices=[Notice("dim", "nothing to undo")])
            if not yes:
                return CommandResult(confirm=Confirm(f"{desc}?", "/undo --yes", "undo cancelled"))
            note = undo_last(env.workdir, env.session_id)
    except Exception as exc:  # noqa: BLE001 - a failed undo must not kill the session
        return CommandResult(notices=[Notice("error", f"/undo failed: {exc}")])
    return CommandResult(notices=[Notice("info", note) if note else Notice("dim", "nothing to undo")])


@register("/redo", "re-apply the last undone turn (git only)", "mutate")
def _redo(env, arg):
    try:
        if not _is_git(env.workdir):
            return CommandResult(notices=[Notice("dim", "redo needs a git repository")])
        from luna.turn.undo import redo as git_redo

        note = git_redo(env.workdir, env.session_id, env.agent, env.thread_id)
    except Exception as exc:  # noqa: BLE001 - a failed redo must not kill the session
        return CommandResult(notices=[Notice("error", f"/redo failed: {exc}")])
    return CommandResult(notices=[Notice("info", note) if note else Notice("dim", "nothing to redo")])


@register("/compact", "summarise and compact the conversation", "mutate")
def _compact(env, arg):
    try:
        ok, message = compact_history(env.agent, env.thread_id)
    except Exception as exc:  # noqa: BLE001 - a failed compact must not kill the session
        return CommandResult(notices=[Notice("error", f"/compact failed: {exc}")])
    if ok:
        forget_messages(env.workdir, env.session_id, _message_count(env))
    if env.index is not None:
        env.index.touch(env.thread_id)
    return CommandResult(notices=[Notice("info" if ok else "error", message)])


@register("/init", "generate or update AGENTS.md", "prompt")
def _init(env, arg):
    env.after_turn_reload()
    return CommandResult(prompt=init_prompt(env.workdir))
```

Place each registration at its `HELP` position (replacing the stubs).

- [ ] **Step 4: Run** new tests + full suite — Expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "feat: registry commands /undo /redo /compact /init"`

---

### Task 10: REPL `dispatch` becomes an adapter over the registry

**Files:**
- Modify: `luna/repl/commands.py` (keep `HELP`, `CommandContext`, `DispatchResult`, `dispatch`; delete the per-command handlers except the REPL-only UI ones and `_init`, `/model` interactive shim)
- Test: existing `tests/test_commands.py` (30 tests) must pass unchanged; add 2 tests

**Interfaces:**
- Consumes: Tasks 7–9.
- Produces: `dispatch(line, ctx) -> DispatchResult` (unchanged signature); `_ReplEnv(ctx)` implementing `CommandEnv`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_commands.py`:

```python
def test_dispatch_goes_through_the_shared_registry(monkeypatch):
    import luna.repl.commands as repl

    seen = []
    real = repl.run_line
    monkeypatch.setattr(repl, "run_line", lambda line, env: seen.append(line) or real(line, env))
    dispatch("/tools", _ctx())
    assert seen == ["/tools"]


def test_confirm_without_input_fn_proceeds_like_before(tmp_path, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_is_git", lambda wd: False)
    monkeypatch.setattr(builtin, "peek_last", lambda wd, sid: "undo x")
    monkeypatch.setattr(builtin, "undo_last", lambda wd, sid: "reverted x")
    ctx = _ctx(workdir=str(tmp_path))
    dispatch("/undo", ctx)
    assert "reverted x" in ctx.console.file.getvalue()
```

- [ ] **Step 2: Run** — Expected: FAIL (`AttributeError: module 'luna.repl.commands' has no attribute 'run_line'`).

- [ ] **Step 3: Rewrite `luna/repl/commands.py`**

Keep the module docstring (updated to say it adapts the shared registry), `HELP` (unchanged dict), `CommandContext`, `DispatchResult`. Keep these REPL-only handlers verbatim: `_help`, `_clear`, `_new`, `_sessions`, `_resume`, `_do_resume`, `_init` (it runs `run_once` + rebuild, the REPL's historical behaviour), plus the interactive part of `_model` (the `choose_model` call). Delete `_tools`, `_agents`, `_usage`, `_provider`, `_compact`, `_add`, `_drop`, `_context`, `_verify`, `_diagnose`, `_diff`, `_undo`, `_redo`, `_commands`, `_plan`, `_reload`, `_TOOL_NAMES`, `_list_tools`, `_list_agents` (move `_TOOL_NAMES` users to `luna.commands.builtin.TOOL_NAMES`; `grep -rn "_TOOL_NAMES\|_list_agents" luna tests` first and update references).

```python
from luna.commands import run_line
from luna.config.usage import SessionUsage
from luna.turn.context import PinnedFiles

_UI: dict[str, Callable] = {
    "/help": _help,
    "/clear": _clear,
    "/new": _new,
    "/sessions": _sessions,
    "/resume": _resume,
    "/init": _init,
}

_STYLE = {"dim": "dim", "ok": "dim", "info": PALETTE["blue"], "warn": "yellow", "error": PALETTE["mauve"]}


class _ReplEnv:
    """:class:`luna.commands.CommandEnv` over the REPL's mutable context."""

    def __init__(self, ctx: CommandContext) -> None:
        self._ctx = ctx
        self.new_agent = None
        self.workdir = ctx.workdir
        self.thread_id = ctx.thread_id
        self.session_id = ctx.session_id
        self.index = ctx.index
        self.user_commands = ctx.user_commands or {}
        if ctx.pinned is None:
            ctx.pinned = PinnedFiles()
        self.pinned = ctx.pinned
        self.usage = ctx.usage if ctx.usage is not None else SessionUsage()

    @property
    def config(self):
        return self._ctx.config

    @property
    def agent(self):
        return self.new_agent if self.new_agent is not None else self._ctx.agent

    @property
    def can_rebuild(self) -> bool:
        return self._ctx.rebuild is not None

    def get_plan(self):
        return None if self._ctx.plan_state is None else self._ctx.plan_state[0]

    def set_plan(self, on: bool) -> None:
        self._ctx.plan_state[0] = on

    def rebuild(self) -> None:
        if self._ctx.rebuild is None:
            raise RuntimeError("not available here")
        self.new_agent = self._ctx.rebuild()

    def _switch(self, provider, model) -> None:
        cfg = self._ctx.config
        previous = (cfg.provider, cfg.model)
        cfg.provider, cfg.model = provider, model
        try:
            self.rebuild()
        except Exception:
            cfg.provider, cfg.model = previous
            raise

    def switch_model(self, model: str) -> None:
        self._switch(self._ctx.config.provider, model)

    def switch_provider(self, provider: str) -> None:
        self._switch(provider, None)

    def after_turn_reload(self) -> None:
        pass  # the REPL's own /init handler rebuilds right after its run_once


def _render(result, ctx: CommandContext, env: _ReplEnv) -> DispatchResult:
    for notice in result.notices:
        ctx.console.print(notice.text, style=_STYLE.get(notice.level, ""), markup=False)
    if result.text:
        ctx.console.print(result.text, markup=False)
    out = DispatchResult(agent=env.new_agent)
    if result.choice is not None and ctx.input_fn is not None:
        picked = arrow_pick(ctx.console, ctx.input_fn, result.choice.options, default=None)
        if picked is not None:
            return dispatch(result.choice.resubmit.format(value=picked), ctx)
    if result.confirm is not None:
        confirmed = True
        if ctx.input_fn is not None:
            confirmed = arrow_confirm(ctx.console, ctx.input_fn, result.confirm.question)
            if confirmed is None:
                answer = ctx.input_fn(f"{result.confirm.question} [y/N] ")
                confirmed = answer.strip().lower() in ("y", "yes")
        if not confirmed:
            ctx.console.print(f"[dim]{result.confirm.cancelled}[/]")
            return out
        return dispatch(result.confirm.resubmit, ctx)
    if result.prompt is not None:
        out.prompt = result.prompt
    return out


def dispatch(line: str, ctx: CommandContext) -> DispatchResult:
    """Route a REPL line through the shared registry. Non-slash lines return ``handled=False``."""
    if not line.startswith("/"):
        return DispatchResult(handled=False)
    name, _, arg = line.partition(" ")
    arg = arg.strip()
    if name in ("/exit", "/quit"):
        return DispatchResult(exit=True)
    if name in _UI:
        return _UI[name](ctx, arg) or DispatchResult()
    if name == "/model" and not arg and ctx.input_fn is not None and ctx.rebuild is not None:
        registry = merge_providers(ctx.config.custom_providers)
        spec = registry[ctx.config.provider]
        api_key = os.environ.get(spec.env_var) if spec.env_var else None
        if not api_key and spec.env_var:
            api_key = get_api_key(ctx.config.provider)
        arg = choose_model(ctx.console, ctx.input_fn, ctx.config.provider, spec, api_key=api_key)
        line = f"/model {arg}"
    env = _ReplEnv(ctx)
    return _render(run_line(line, env), ctx, env)
```

`_help` prints `HELP` as before (unchanged). If `tests/test_commands.py` asserts exact old markup for a moved command (e.g. `[{PALETTE['blue']}]model → x`) the plain text is identical; only Rich styling differs — if a test compares raw ANSI output, note a Ruling and compare stripped text instead.

- [ ] **Step 4: Run** `tests/test_commands.py`, `tests/test_repl_flow.py`, full suite — Expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "refactor: REPL slash commands run through the shared registry"`

---

### Task 11: Server command API, command list and session state

**Files:**
- Create: `luna/server/commands.py`
- Modify: `luna/server/app.py` (routes), `luna/server/client.py` (`run_command`, `list_commands`, `get_state`; error texts)
- Test: `tests/test_server_commands.py`

**Interfaces:**
- Consumes: Tasks 5–10.
- Produces:
  - `POST /sessions/{thread_id}/command {workdir, line}` → `CommandResult.to_json()`
  - `GET /commands?workdir=` → `{"commands": [{name, help, kind}]}`
  - `GET /sessions/{thread_id}/state?workdir=` → `{"provider", "model", "plan", "pinned": [..], "usage_summary": str}`
  - `ServerClient.run_command(thread_id, line, workdir) -> dict`, `.list_commands(workdir) -> list[dict]`, `.get_state(thread_id, workdir) -> dict`
  - client `_ERROR_TEXT` gains `session_busy` → "Дождитесь окончания ответа, затем повторите.", `client_command` → "Эту команду выполняет сам TUI.", `workdir_mismatch` → "Сессия принадлежит другой папке."

- [ ] **Step 1: Write the failing tests** — `tests/test_server_commands.py`:

```python
import asyncio

import httpx
import pytest
from langchain_core.messages import AIMessage

from luna.server.app import create_app
from luna.server.run import make_session_agent_factory


@pytest.fixture
def api(tmp_path):
    from tests.conftest import FakeToolCallingModel

    app = create_app(
        token="t",
        session_agent_factory=make_session_agent_factory(
            model=FakeToolCallingModel(responses=[AIMessage(content="ok")])
        ),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer t"}
    )
    return client, app, str(tmp_path)


async def _cmd(c, wd, line, thread="t1"):
    return await c.post(f"/sessions/{thread}/command", json={"workdir": wd, "line": line})


async def test_plan_command_changes_only_that_session(api):
    c, app, wd = api
    async with c:
        resp = await _cmd(c, wd, "/plan")
        state_one = (await c.get("/sessions/t1/state", params={"workdir": wd})).json()
        state_two = (await c.get("/sessions/t2/state", params={"workdir": wd})).json()
    assert resp.json()["notices"] == [{"level": "info", "text": "plan mode: on"}]
    assert state_one["plan"] is True and state_two["plan"] is False


async def test_confirm_round_trip_for_undo(api, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_is_git", lambda wd: False)
    monkeypatch.setattr(builtin, "peek_last", lambda wd, sid: "undo x")
    monkeypatch.setattr(builtin, "undo_last", lambda wd, sid: "reverted x")
    c, _, wd = api
    async with c:
        first = (await _cmd(c, wd, "/undo")).json()
        second = (await _cmd(c, wd, first["confirm"]["resubmit"])).json()
    assert first["confirm"]["question"] == "undo x?"
    assert second["notices"] == [{"level": "info", "text": "reverted x"}]


async def test_ui_commands_are_refused_by_the_server(api):
    c, _, wd = api
    async with c:
        resp = await _cmd(c, wd, "/help")
    assert resp.status_code == 400 and resp.json() == {"error": "client_command"}


async def test_mutate_is_409_while_streaming_but_read_is_not(api):
    c, app, wd = api
    async with c:
        runtime = app.state.runtimes.get("t1", wd)
        await runtime.lock.acquire()
        try:
            busy = await _cmd(c, wd, "/plan")
            read = await _cmd(c, wd, "/context")
        finally:
            runtime.lock.release()
    assert busy.status_code == 409
    assert read.status_code == 200


async def test_read_command_answers_while_a_turn_streams(api, monkeypatch):
    """The sync graph must not block the event loop: /usage answers mid-turn."""
    import time

    from luna.server import turns

    started = asyncio.Event()
    real = turns._graph_events

    def slow_graph(*a, **k):
        started_loop.call_soon_threadsafe(started.set)
        time.sleep(1.0)
        yield from real(*a, **k)

    monkeypatch.setattr(turns, "_graph_events", slow_graph)
    c, _, wd = api
    started_loop = asyncio.get_running_loop()
    async with c:
        turn = asyncio.create_task(
            c.post("/sessions/t1/messages", json={"workdir": wd, "content": "hi"})
        )
        await asyncio.wait_for(started.wait(), 5)
        t0 = time.monotonic()
        resp = await _cmd(c, wd, "/usage")
        elapsed = time.monotonic() - t0
        await turn
    assert resp.status_code == 200 and elapsed < 0.8


async def test_commands_list_includes_user_commands(api, tmp_path):
    (tmp_path / ".luna" / "commands").mkdir(parents=True)
    (tmp_path / ".luna" / "commands" / "ship.md").write_text("---\ndescription: ship it\n---\nship $ARGUMENTS\n")
    c, _, wd = api
    async with c:
        cmds = (await c.get("/commands", params={"workdir": wd})).json()["commands"]
    assert {"name": "/ship", "help": "ship it", "kind": "prompt"} in cmds
    assert any(x["name"] == "/model" and x["kind"] == "mutate" for x in cmds)
```

(The `.luna/commands` frontmatter format must match `luna/repl/usercmd.py::_frontmatter`; check it and adjust the fixture text if its key differs.)

- [ ] **Step 2: Run** — Expected: FAIL (404 on the new routes).

- [ ] **Step 3: Implement `luna/server/commands.py`**

```python
"""Slash commands over HTTP: one entry point, like opencode's /session/:id/command."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from luna.commands import command_kind, list_commands, run_line
from luna.config.usage import indicator_line
from luna.repl import usercmd
from luna.server.runtime import runtime_for
from luna.server.trust import trust_error


class _ServerEnv:
    """:class:`luna.commands.CommandEnv` over a :class:`SessionRuntime`."""

    def __init__(self, runtime) -> None:
        self._rt = runtime
        self.workdir = runtime.workdir
        self.thread_id = runtime.thread_id
        self.session_id = runtime.thread_id
        from luna.core.persistence import SessionIndex

        self.index = SessionIndex()
        self.user_commands = usercmd.load(runtime.workdir)
        self.pinned = runtime.state.pinned
        self.usage = runtime.state.usage
        self._config = None

    @property
    def config(self):
        if self._config is None:
            self._config = self._rt.config()
        return self._config

    @property
    def agent(self):
        return self._rt.agent

    can_rebuild = True

    def get_plan(self):
        return self._rt.state.plan

    def set_plan(self, on: bool) -> None:
        self._rt.set_plan(on)

    def rebuild(self) -> None:
        self._rt.rebuild()

    def switch_model(self, model: str) -> None:
        self._rt.switch_model(model)
        self._config = None

    def switch_provider(self, provider: str) -> None:
        self._rt.switch_provider(provider)
        self._config = None

    def after_turn_reload(self) -> None:
        self._rt.reload_after_turn = True


async def post_command(request: Request) -> JSONResponse:
    """Run one slash command for a session and return its structured result."""
    thread_id = request.path_params["thread_id"]
    body = await request.json()
    if (error := trust_error(request, body.get("workdir"))) is not None:
        return error
    runtime = runtime_for(request, thread_id, body.get("workdir", "."))
    if isinstance(runtime, JSONResponse):
        return runtime
    line = body["line"].strip()
    name = line.partition(" ")[0]
    kind = command_kind(name, usercmd.load(runtime.workdir))
    if kind == "ui":
        return JSONResponse({"error": "client_command"}, status_code=400)
    if kind != "read" and runtime.lock.locked():
        return JSONResponse({"error": "session_busy"}, status_code=409)

    def _run():
        result = run_line(line, _ServerEnv(runtime))
        runtime.save()
        return result

    result = await run_in_threadpool(_run)
    return JSONResponse(result.to_json())


async def get_commands(request: Request) -> JSONResponse:
    """Built-in + project commands, for the TUI's /help and autocomplete."""
    workdir = request.query_params.get("workdir")
    if (error := trust_error(request, workdir)) is not None:
        return error
    return JSONResponse({"commands": list_commands(usercmd.load(workdir or "."))})


async def get_state(request: Request) -> JSONResponse:
    """The session's settings for the status bar."""
    thread_id = request.path_params["thread_id"]
    workdir = request.query_params.get("workdir")
    if (error := trust_error(request, workdir)) is not None:
        return error
    runtime = runtime_for(request, thread_id, workdir or ".")
    if isinstance(runtime, JSONResponse):
        return runtime
    cfg = await run_in_threadpool(runtime.config)
    state = runtime.state
    summary = (
        indicator_line(state.usage, cfg.provider, cfg.model, cfg.pricing) if state.usage.turns else ""
    )
    return JSONResponse(
        {
            "provider": cfg.provider,
            "model": cfg.model,
            "plan": state.plan,
            "pinned": state.pinned.paths,
            "usage_summary": summary,
        }
    )
```

Routes in `create_app`:

```python
            Route("/commands", get_commands, methods=["GET"]),
            Route("/sessions/{thread_id}/command", post_command, methods=["POST"]),
            Route("/sessions/{thread_id}/state", get_state, methods=["GET"]),
```

Client methods (`luna/server/client.py`):

```python
    async def run_command(self, thread_id: str, line: str, workdir: str) -> dict:
        """Run a slash command; returns the structured CommandResult JSON."""
        resp = await self._http.post(
            f"/sessions/{thread_id}/command",
            json={"line": line, "workdir": workdir},
            headers=self._auth_headers(),
            timeout=_STREAM_TIMEOUT,  # /compact and /verify can take a while
        )
        _check(resp)
        return resp.json()

    async def list_commands(self, workdir: str) -> list[dict]:
        """Built-in + project slash commands."""
        resp = await self._http.get("/commands", params={"workdir": workdir}, headers=self._auth_headers())
        _check(resp)
        return resp.json()["commands"]

    async def get_state(self, thread_id: str, workdir: str) -> dict:
        """Session settings for the status bar."""
        resp = await self._http.get(
            f"/sessions/{thread_id}/state", params={"workdir": workdir}, headers=self._auth_headers()
        )
        _check(resp)
        return resp.json()
```

Add the three `_ERROR_TEXT` entries listed in Interfaces. Wrap non-stream methods' transport errors like `_stream` does (`httpx.TransportError` → `ServerError("Связь с сервером Luna прервалась: …")`) via a small `_request` helper used by all of them, with a test in `tests/test_server_client.py` mirroring `test_a_dropped_stream_becomes_a_readable_server_error` for `list_sessions`.

- [ ] **Step 4: Run** the new tests, `tests/test_server_client.py`, full suite — Expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "feat: server API for slash commands, command list and session state"`

---

### Task 12: TUI — command routing, pickers, notices, local commands

**Files:**
- Create: `luna/tui/pickers.py`
- Modify: `luna/tui/chat.py` (`NoticeRow`, `_run_command`, `_render_command_result`, local `ui` commands, SSE `notice`), `luna/tui/app.py` (`open_session_picker`), `luna/tui/luna.tcss`
- Test: `tests/test_tui_commands.py` (new); adjust `tests/test_tui_chat.py` fakes (base class) and the three "local command" tests

**Interfaces:**
- Consumes: Task 11 client methods.
- Produces:
  - `ChoiceModal(title: str, options: list[tuple[str, str]])` → dismisses with the value or `None`
  - `ConfirmModal(question: str)` → dismisses with `bool`
  - `NoticeRow(level: str, text: str)` widget
  - `ChatPane.submit(content: str) -> None` (awaitable; the Textual handler starts it in a worker — Task 13)
  - `LunaApp.open_session_picker(arg: str = "") -> None`

- [ ] **Step 1: Write the failing tests** — `tests/test_tui_commands.py`:

```python
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from luna.tui.chat import ChatPane, NoticeRow
from luna.tui.pickers import ChoiceModal, ConfirmModal


class _Client:
    def __init__(self, results):
        self.results = list(results)
        self.lines = []

    async def get_history(self, thread_id, workdir):
        return []

    async def get_state(self, thread_id, workdir):
        return {"provider": "p", "model": "m", "plan": False, "pinned": [], "usage_summary": ""}

    async def list_commands(self, workdir):
        return [{"name": "/model", "help": "pick", "kind": "mutate"}, {"name": "/usage", "help": "u", "kind": "read"}]

    async def run_command(self, thread_id, line, workdir):
        self.lines.append(line)
        return self.results.pop(0)


class _Host(App):
    def __init__(self, client):
        super().__init__()
        self.client = client

    def compose(self) -> ComposeResult:
        yield ChatPane(workdir="/w", thread_id="t1", provider="p", model="m")


def _res(**kw):
    base = {"text": "", "notices": [], "choice": None, "confirm": None, "prompt": None, "effects": {}}
    base.update(kw)
    return base


async def test_notices_and_text_render_in_the_transcript():
    client = _Client([_res(text="**hi**", notices=[{"level": "info", "text": "plan mode: on"}])])
    app = _Host(client)
    async with app.run_test():
        chat = app.query_one(ChatPane)
        await chat.submit("/plan")
        rows = chat.query_one("#transcript", VerticalScroll).query(NoticeRow)
        assert [r.text for r in rows] == ["plan mode: on"]
    assert client.lines == ["/plan"]


async def test_choice_opens_a_picker_and_resubmits():
    client = _Client([
        _res(choice={"title": "Модель", "options": [["a", "a"], ["b", "b"]], "resubmit": "/model {value}"}),
        _res(notices=[{"level": "info", "text": "model → b"}]),
    ])
    app = _Host(client)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        chat.run_worker(chat.submit("/model"))
        await pilot.pause()
        assert isinstance(app.screen, ChoiceModal)
        await pilot.press("down", "enter")
        await pilot.pause()
    assert client.lines == ["/model", "/model b"]


async def test_confirm_no_shows_cancelled():
    client = _Client([_res(confirm={"question": "undo x?", "resubmit": "/undo --yes", "cancelled": "undo cancelled"})])
    app = _Host(client)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        chat.run_worker(chat.submit("/undo"))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("escape")
        await pilot.pause()
        rows = chat.query(NoticeRow)
        assert [r.text for r in rows] == ["undo cancelled"]
    assert client.lines == ["/undo"]


async def test_prompt_result_is_sent_as_a_message():
    sent = []

    class _PromptClient(_Client):
        async def send_message(self, thread_id, content, workdir):
            sent.append(content)
            yield {"event": "turn_done"}

    app = _Host(_PromptClient([_res(prompt="WRITE AGENTS.md")]))
    async with app.run_test():
        await app.query_one(ChatPane).submit("/init")
    assert sent == ["WRITE AGENTS.md"]


async def test_help_is_local_and_lists_server_commands():
    client = _Client([])
    app = _Host(client)
    async with app.run_test():
        chat = app.query_one(ChatPane)
        await chat.submit("/help")
    assert client.lines == []
```

- [ ] **Step 2: Run** — Expected: FAIL (`ImportError: cannot import name 'NoticeRow'`).

- [ ] **Step 3: Implement `luna/tui/pickers.py`**

```python
"""Modal pickers for command results: a choice list and a yes/no confirm."""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static


class ChoiceModal(ModalScreen[str | None]):
    """Pick one option (↑↓ + Enter); Esc dismisses with ``None``."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, title: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self):
        with Vertical():
            yield Static(self._title, classes="picker-title")
            items = []
            for value, label in self._options:
                item = ListItem(Label(label))
                item.data_value = value
                items.append(item)
            yield ListView(*items, id="picker-list")
            yield Static("↑↓ выбрать · Enter подтвердить · Esc отмена", classes="picker-hint")

    def on_mount(self) -> None:
        self.query_one("#picker-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "data_value", None))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """Yes/No question; Esc means No."""

    BINDINGS = [Binding("escape", "no", "Нет")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self):
        with Vertical():
            yield Static(self._question, classes="picker-title")
            yield Button("Да", id="confirm-yes", variant="warning")
            yield Button("Нет", id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_no(self) -> None:
        self.dismiss(False)
```

CSS in `luna.tcss` (next to the approval modal rules):

```css
ChoiceModal, ConfirmModal {
    align: center middle;
}

ChoiceModal > Vertical, ConfirmModal > Vertical {
    width: 60%;
    height: auto;
    max-height: 70%;
    background: $bg;
    border: round $peri;
    padding: 1 2;
}

ChoiceModal #picker-list {
    height: auto;
    max-height: 20;
    background: $bg;
}

.picker-title {
    text-style: bold;
    padding-bottom: 1;
}

.picker-hint {
    color: $moon-dim;
    padding-top: 1;
}

NoticeRow {
    margin: 0 1 1 3;
}
```

- [ ] **Step 4: Implement command handling in `luna/tui/chat.py`**

Add `NoticeRow`:

```python
_NOTICE_COLOURS = {
    "dim": TUI_VARIABLES["moon-dim"],
    "ok": TUI_VARIABLES["moon-dim"],
    "info": TUI_VARIABLES["peri"],
    "warn": "#e8c37a",
    "error": TUI_VARIABLES["err"],
}


class NoticeRow(Static):
    """A short status line (command result, verify, formatter, reload)."""

    def __init__(self, level: str, text: str) -> None:
        super().__init__(Text(text, style=_NOTICE_COLOURS.get(level, "")))
        self.level = level
        self.text = text
```

Replace `_run_local_command` with the following (keep `_LOCALLY_SUPPORTED` deleted; `/exit`, `/quit`, `/clear` keep their behaviour):

```python
    _LOCAL = ("/exit", "/quit", "/clear", "/help", "/new", "/sessions", "/resume")

    async def _run_command(self, line: str) -> None:
        """Run a slash command: client-side ones here, the rest on the server."""
        transcript = self.query_one("#transcript", VerticalScroll)
        name, _, arg = line.partition(" ")
        if name in ("/exit", "/quit"):
            self.app.exit()
            return
        if name == "/clear":
            await transcript.remove_children()
            return
        await transcript.mount(UserMessage(line))
        if name == "/help":
            await transcript.mount(SystemMessage(self._help_markdown()))
        elif name == "/new":
            await self.app.action_new_session()
        elif name in ("/sessions", "/resume"):
            await self.app.open_session_picker(arg.strip())
        else:
            try:
                result = await self.app.client.run_command(self.thread_id, line, self._workdir)
            except ServerError as exc:
                await transcript.mount(NoticeRow("error", str(exc)))
            else:
                await self._render_command_result(result)
        transcript.anchor()

    async def _render_command_result(self, result: dict) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        for notice in result.get("notices", []):
            await transcript.mount(NoticeRow(notice["level"], notice["text"]))
        if result.get("text"):
            await transcript.mount(SystemMessage(result["text"]))
        transcript.anchor()
        if result.get("choice"):
            choice = result["choice"]
            options = [tuple(o) for o in choice["options"]]
            picked = await self.app.push_screen_wait(ChoiceModal(choice["title"], options))
            if picked is not None:
                await self._run_command(choice["resubmit"].format(value=picked))
        elif result.get("confirm"):
            confirm = result["confirm"]
            if await self.app.push_screen_wait(ConfirmModal(confirm["question"])):
                await self._run_command(confirm["resubmit"])
            else:
                await transcript.mount(NoticeRow("dim", confirm["cancelled"]))
        elif result.get("prompt"):
            await self._send(result["prompt"], echo=None)
        if result.get("effects"):
            await self.refresh_state()

    def _help_markdown(self) -> str:
        groups = {"ui": "Интерфейс", "read": "Просмотр", "mutate": "Изменения", "prompt": "Задачи агенту"}
        lines = ["**Команды**"]
        for kind, title in groups.items():
            items = [c for c in self._commands if c["kind"] == kind]
            if items:
                lines.append(f"\n*{title}*\n")
                lines += [f"- `{c['name']}` — {c['help']}" for c in items]
        return "\n".join(lines)
```

(`push_screen_wait` needs worker context: `submit` is always run in a worker by the input handler (Task 13) and by the tests above via `run_worker`; the tests that `await chat.submit(...)` directly never reach a modal.)

Split the existing message path out of `on_input_submitted` into `_send(content: str, echo: str | None)` — same body as today from "Echo the user's own message" to the end, mounting `UserMessage(echo)` only when `echo` is not `None` — and add:

```python
    async def submit(self, content: str) -> None:
        """Handle one submitted line: a slash command or a chat message."""
        if content.startswith("/"):
            await self._run_command(content)
        else:
            await self._send(content, echo=content)
```

Fetch the command list on mount (`self._commands = await self.app.client.list_commands(self._workdir)`, falling back to `[{"name": n, "help": h, "kind": "mutate"} for n, h in HELP.items()]` on `ServerError`/`AttributeError`-free path — see Task 13 for the fake-client base class) and render SSE notices in `_apply_event`:

```python
        elif evt["event"] == "notice":
            await reply.notice(evt["level"], evt["text"])
```

with `_LiveReply.notice` = `await self._end_text_block(); await self._mount(NoticeRow(level, text))`.

`LunaApp.open_session_picker` (`luna/tui/app.py`):

```python
    async def open_session_picker(self, arg: str = "") -> None:
        """/sessions and /resume: pick a session of this project (or /resume <n>)."""
        try:
            sessions = await self.client.list_sessions(self._workdir)
        except ServerError as exc:
            self.notify(str(exc), severity="error")
            return
        if arg.isdigit() and 1 <= int(arg) <= len(sessions):
            await self._open_thread(sessions[int(arg) - 1]["thread_id"])
            return
        options = [(s["thread_id"], f"{s['title']}  ·  {s['relative_time']}") for s in sessions]
        picked = await self.push_screen_wait(ChoiceModal("Сессии", options))
        if picked:
            await self._open_thread(picked)
```

Delete the `/help`, `/quit`, `/clear`, `/undo`-"not yet in the TUI" expectations in `tests/test_tui_chat.py` that no longer hold: `/quit` and `/clear` tests stay as they are; the `/help` test now asserts a `SystemMessage` containing "**Команды**"; the "`/undo` is not yet in the TUI" test is replaced by `test_notices_and_text_render_in_the_transcript` above — delete it.

- [ ] **Step 5: Run** `tests/test_tui_commands.py`, all `tests/test_tui_*.py`, full suite — Expected: all pass.

- [ ] **Step 6: Commit** — `git commit -m "feat: TUI runs every slash command: pickers, confirms, notices, local session picker"`

---

### Task 13: TUI — turns in a worker, busy rules, status bar, plan border, autocomplete

**Files:**
- Modify: `luna/tui/chat.py` (`on_input_submitted`, `refresh_state`, status bar source), `luna/tui/status_bar.py` (`pinned`), `luna/tui/commands.py` (`filter_commands(prefix, commands)`), `luna/tui/app.py` (`_open_thread` → `refresh_state`), `luna/tui/luna.tcss` (plan border)
- Test: `tests/test_tui_commands.py` (append); `tests/test_tui_chat.py` (base fake + call-site migration), `tests/test_tui_status_bar.py`

**Interfaces:**
- Consumes: Tasks 11–12.
- Produces: `ChatPane.refresh_state() -> None`; `format_status_line(..., pinned: int = 0)`; `filter_commands(prefix: str, commands: list[dict]) -> list[tuple[str, str]]`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_tui_commands.py`:

```python
from textual.widgets import Input

from luna.tui.status_bar import format_status_line


def test_status_line_shows_pinned_count():
    line = format_status_line(model="m", cost_usd=0, context_file=None, plan_mode=True, undo_depth=0, pinned=2)
    assert "plan: on" in line and "pinned: 2" in line
    assert "pinned" not in format_status_line(model="m", cost_usd=0, context_file=None, plan_mode=False, undo_depth=0)


async def test_plan_state_colours_the_input_border():
    client = _Client([])

    async def plan_state(thread_id, workdir):
        return {"provider": "p", "model": "m", "plan": True, "pinned": ["a"], "usage_summary": "ctx 1k"}

    client.get_state = plan_state
    app = _Host(client)
    async with app.run_test():
        chat = app.query_one(ChatPane)
        await chat.refresh_state()
        assert chat.has_class("-plan")
        bar = chat.query_one("#status-bar")
        assert bar.plan_mode is True and bar.pinned == 1 and bar.usage_summary == "ctx 1k"


async def test_read_command_runs_mid_turn_but_a_message_is_refused():
    client = _Client([_res(notices=[{"level": "info", "text": "no usage recorded yet"}])])
    app = _Host(client)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        chat.busy = True
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "/usage"))
        await pilot.pause()
        await chat.on_input_submitted(Input.Submitted(inp, "hello"))
        await pilot.pause()
    assert client.lines == ["/usage"]


async def test_autocomplete_uses_the_server_command_list():
    from luna.tui.commands import filter_commands

    cmds = [{"name": "/ship", "help": "ship it", "kind": "prompt"}, {"name": "/model", "help": "m", "kind": "mutate"}]
    assert filter_commands("/sh", cmds) == [("/ship", "ship it")]
```

- [ ] **Step 2: Run** — Expected: FAIL.

- [ ] **Step 3: Implement**

`luna/tui/commands.py`:

```python
"""Slash-command filtering for the TUI's input autocomplete."""

from __future__ import annotations

from luna.repl.commands import HELP

#: Used until the server's list arrives (or if it cannot be fetched).
FALLBACK_COMMANDS = [{"name": n, "help": h, "kind": "mutate"} for n, h in HELP.items()]


def filter_commands(prefix: str, commands: list[dict] | None = None) -> list[tuple[str, str]]:
    """Return (name, help) pairs whose name starts with ``prefix``."""
    source = commands if commands is not None else FALLBACK_COMMANDS
    return [(c["name"], c["help"]) for c in source if c["name"].startswith(prefix)]
```

(`on_input_changed` calls `filter_commands(value, self._commands)`; existing filter tests call `filter_commands("/cle")` and keep working through the default.)

`luna/tui/status_bar.py`: add `pinned: int = 0` parameter to `format_status_line`, appended after `plan` as `parts.append(f"pinned: {pinned}")` only when `pinned > 0`; add `pinned: reactive[int] = reactive(0)` and pass it in `render`.

`ChatPane`:

```python
    async def refresh_state(self) -> None:
        """Pull this session's settings from the server into the status bar."""
        try:
            state = await self.app.client.get_state(self.thread_id, self._workdir)
        except ServerError as exc:
            self.notify(str(exc), severity="error")
            return
        bar = self.query_one(StatusBar)
        bar.provider = state["provider"] or ""
        bar.model = state["model"] or ""
        bar.plan_mode = bool(state["plan"])
        bar.pinned = len(state["pinned"])
        bar.usage_summary = state["usage_summary"]
        self.set_class(bool(state["plan"]), "-plan")

    def _allowed_while_busy(self, line: str) -> bool:
        name = line.partition(" ")[0]
        if name in ("/help", "/clear", "/exit", "/quit"):
            return True
        return any(c["name"] == name and c["kind"] == "read" for c in self._commands)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Start the line in a worker so input stays live during a turn."""
        if event.input.id != "chat-input":
            return
        if self._accept_highlighted_command():
            return
        content = event.value
        event.input.value = ""
        self.query_one("#autocomplete", ListView).display = False
        if not content.strip():
            return
        if self.busy and not self._allowed_while_busy(content):
            self.notify("Дождитесь окончания ответа.", severity="warning")
            return
        self.run_worker(self.submit(content), group="chat", exit_on_error=False)
```

In `on_mount`: after `load_history`, `self._commands = await self._load_commands()` and `await self.refresh_state()`; `_load_commands` returns `FALLBACK_COMMANDS` on `ServerError`. On `turn_done` in `_apply_event`: replace the client-side `self._session_usage` accumulation with `await self.refresh_state()` (delete `_session_usage`, `TurnUsage` merging and `_refresh_status_bar`'s usage branch; `usage_delta` events are ignored by the TUI now — the server accumulates). `LunaApp._open_thread`: after `load_history`, `await chat.refresh_state()`.

CSS:

```css
ChatPane.-plan #chat-input,
ChatPane.-plan #chat-input:focus {
    border: round $mauve;
}
```

**Test migration** (`tests/test_tui_chat.py`): add

```python
class _BaseFakeClient:
    async def get_history(self, thread_id, workdir):
        return []

    async def get_state(self, thread_id, workdir):
        return {"provider": "anthropic", "model": "claude-sonnet-5", "plan": False, "pinned": [], "usage_summary": ""}

    async def list_commands(self, workdir):
        from luna.tui.commands import FALLBACK_COMMANDS

        return FALLBACK_COMMANDS

    async def run_command(self, thread_id, line, workdir):
        raise AssertionError(f"unexpected command {line!r}")
```

make every fake client class in the file inherit from it, and replace every `await chat.on_input_submitted(Input.Submitted(inp, X))` / `asyncio.create_task(chat.on_input_submitted(Input.Submitted(inp, X)))` with `await chat.submit(X)` / `asyncio.create_task(chat.submit(X))` — **except** the autocomplete-acceptance test (`sent = await chat.on_input_submitted(...)`), which exercises the handler itself. Rewrite `test_usage_delta_events_update_the_status_bar_once_the_turn_completes` so its fake's `get_state` returns `usage_summary="ctx ~1k/200k"` after the turn, asserting the status bar shows it. Apply the same `submit` change to the one call site each in `tests/test_tui_app.py` and `tests/test_tui_sidebars.py`.

- [ ] **Step 4: Run** all `tests/test_tui_*.py`, full suite, `uv run ruff check luna tests` — Expected: all pass, clean.

- [ ] **Step 5: Commit** — `git commit -m "feat: TUI turns in a worker, read commands mid-turn, session state in the status bar, plan-mode border"`

---

### Task 14: Live check, critique/polish, changelog, notes

**Files:**
- Modify: `CHANGELOG.md` (`## [Unreleased]`, Russian, existing sections)

- [ ] **Step 1: Live pty run** (driver: the `pyte` pty script used on 2026-09-28 — `uv run --with pyte python <scratchpad>/drive.py <workdir> '<steps json>' uv run luna --no-splash`):
  - `/help` → grouped list incl. project commands; `/tools`, `/usage`, `/context` answer.
  - `/plan` → notice + mauve input border + `plan: on`; a write request is refused by the agent's guard; `/plan off`.
  - `/model` → picker; Esc cancels; pick → notice `model → …`, status bar updates; open a second session (Ctrl+N) → it still has the old model.
  - `/add README.md` → `pinned: 1` in the status bar; ask "what is pinned?" → the answer uses it.
  - Ask for a small file edit → tool rows, then notices (format/diagnose/verify if configured); `/diff`; `/undo` → confirm modal → revert notice.
  - During a long answer type `/usage` → answers immediately; type `hello` → "Дождитесь окончания ответа".
  - `/sessions` → picker; `/resume 2` opens the 2nd session.
  - REPL sanity (`echo "/tools" | uv run luna --no-input`) still prints the tool list.
- [ ] **Step 2:** Run the `critique` skill, then the `polish` skill on the live screen; fix only `luna/tui` styles/widgets; rerun the suite.
- [ ] **Step 3: Changelog** — under `### Добавлено`: all slash commands in the TUI (pickers/confirm, local `/help` `/sessions` `/resume`), per-session model/provider/plan/pinned/usage stored in `luna_session_state`, full REPL turn pipeline on the server (undo journal, `/add`, `@file`, `@agent`, format/diagnose/verify + fix-up, auto-reload), read commands mid-turn, plan-mode border; under `### Изменено`: one agent per session on the server, REPL commands run through a shared registry, TUI turns run in a worker; under `### Исправлено`: TUI undo/plan never worked (server agent had no `session_id` / `plan_flag`), server event loop blocked during a turn.
- [ ] **Step 4: Commit** — `git commit -m "docs: changelog for TUI slash commands and turn parity"`; append a progress entry to the Obsidian note `Projects/Luna/luna-simple — CLI-агент на deepagents.md`.
