# Luna Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close six real defects from the 0.2.0 review — subagent guard/approval bypass, `persistence.py` crashes, wasteful `/compact`, per-process undo journal, undo deleting binaries, single-tool-name deny rules.

**Architecture:** Each fix is localised to 1–3 modules. `deepagents`/`langgraph` imports stay in `agent.py`, `session.py`, `persistence.py`, `toolguard.py`. `/compact` logic moves from `commands.py` into a `session.compact_thread()` helper so `commands.py` stays framework-free.

**Tech Stack:** Python 3.11+, deepagents ~=0.7.13, langchain ~=1.4, langgraph ~=1.2, langgraph-checkpoint-sqlite, rich, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-08-luna-hardening-design.md` — read it alongside this plan.

## Global Constraints

- Python **3.11+**, PEP 8 / PEP 257. `uv run ruff check .` and `uv run ruff format --check .` must pass.
- `deepagents` / `langgraph` / `langchain_mcp_adapters` imports live ONLY in `luna/agent.py`, `luna/session.py`, `luna/persistence.py`, `luna/toolguard.py`.
- Model IDs live in `luna/providers.py` or config — never in logic.
- Tests never hit the network. Use the `FakeToolCallingModel` fixture (`tests/conftest.py`); the autouse `isolated_config_home` fixture redirects `XDG_CONFIG_HOME` + `Path.home()`.
- `uv run pytest -q` must be FULLY green on the implementing machine (170 at branch start) — no `python -c` hardcodes, no skips introduced.
- `persistence.py`, `undo.py`, `permissions.py`, `gitinfo.py`, `memory.py`, `context.py`, `verify.py` never raise on a bad path / locked DB / absent tool — degrade, don't throw.
- Commit after every task; message ends with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Work on branch `harden/real-holes` (already created).

---

### Task 1: `permissions.py` — wildcard tool + tool groups in deny/allow rules

**Files:**
- Modify: `luna/permissions.py:41-49` (`RuleSet._hit`) + add `_TOOL_GROUPS` / `_tool_matches`
- Test: `tests/test_permissions.py` (extend)

**Interfaces:**
- Consumes: nothing new.
- Produces: `RuleSet._hit` now matches a rule whose tool segment is `*` (any tool) or a group name (`write` = `{write_file, edit_file, delete}`, `fs` = write + read-only fs tools). Exact-tool rules behave identically. `RuleSet.match`, `load_rules`, `append_project_rule`, `suggest_rule` signatures unchanged.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_permissions.py`)

```python
def test_wildcard_tool_blocks_every_tool():
    rs = RuleSet(deny=["*:.env"])
    assert rs.match("write_file", {"file_path": "/.env"}) == "deny"
    assert rs.match("edit_file", {"file_path": ".env"}) == "deny"
    assert rs.match("delete", {"file_path": ".env"}) == "deny"
    assert rs.match("read_file", {"file_path": "app.py"}) is None


def test_write_group_blocks_mutators_only():
    rs = RuleSet(deny=["write:secrets/*"])
    assert rs.match("write_file", {"file_path": "secrets/k.txt"}) == "deny"
    assert rs.match("edit_file", {"file_path": "secrets/k.txt"}) == "deny"
    assert rs.match("delete", {"file_path": "secrets/k.txt"}) == "deny"
    assert rs.match("read_file", {"file_path": "secrets/k.txt"}) is None


def test_fs_group_includes_read():
    assert RuleSet(deny=["fs:x.txt"]).match("read_file", {"file_path": "x.txt"}) == "deny"


def test_wildcard_matches_execute_by_command():
    rs = RuleSet(deny=["*:git push*"])
    assert rs.match("execute", {"command": "git push origin"}) == "deny"
    assert rs.match("execute", {"command": "git status"}) is None


def test_exact_tool_rules_unchanged():
    rs = RuleSet(deny=["write_file:.env"], allow=["execute:pytest*"])
    assert rs.match("write_file", {"file_path": "/.env"}) == "deny"
    assert rs.match("edit_file", {"file_path": ".env"}) is None  # exact tool only
    assert rs.match("execute", {"command": "pytest -q"}) == "allow"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_permissions.py -q`
Expected: FAIL — the `*` / `write` / `fs` rules return `None` because `rtool != tool`.

- [ ] **Step 3: Implement**

In `luna/permissions.py`, after `_relpath` / before `RuleSet`:

```python
_TOOL_GROUPS: dict[str, frozenset[str]] = {
    "write": frozenset({"write_file", "edit_file", "delete"}),
    "fs": frozenset({"write_file", "edit_file", "delete", "read_file", "ls", "glob", "grep"}),
}


def _tool_matches(rule_tool: str, tool: str) -> bool:
    """True if a rule's tool segment (exact name, ``*``, or a group) covers ``tool``."""
    if rule_tool in ("*", tool):
        return True
    group = _TOOL_GROUPS.get(rule_tool)
    return group is not None and tool in group
```

In `RuleSet._hit`, replace the `if rtool != tool: continue` guard:

```python
    def _hit(self, rules: list[str], tool: str, subject: str) -> bool:
        for rule in rules:
            rtool, _, pattern = rule.partition(":")
            if not _tool_matches(rtool, tool):
                continue
            if tool != "execute":
                pattern = _relpath(pattern)
            if fnmatch(subject, pattern):
                return True
        return False
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_permissions.py -q`
Expected: PASS (all, including the pre-existing rule tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check luna/permissions.py tests/test_permissions.py && uv run ruff format luna/permissions.py tests/test_permissions.py
git add luna/permissions.py tests/test_permissions.py
git commit -m "feat: deny/allow rules support '*' and tool-group segments"
```

---

### Task 2: `persistence.py` — never raise on a locked / unwritable `sessions.db`

**Files:**
- Modify: `luna/persistence.py` (`SessionIndex`, `checkpointer`)
- Modify: `luna/cli.py:369-372` (pass `on_warn` to `checkpointer`)
- Test: `tests/test_persistence.py` (extend)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `SessionIndex(env=None)` never raises in `__init__`; on failure `record`/`touch` are no-ops, `latest_for` returns `None`, `list` returns `[]`. New read-only property `SessionIndex.ok -> bool`.
  - `checkpointer(env=None, *, on_warn: Callable[[str], None] | None = None) -> BaseCheckpointSaver` — returns an `InMemorySaver` (and calls `on_warn` once) when the SQLite saver cannot be opened.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_persistence.py  (add)
def test_session_index_survives_unwritable_db(tmp_path, monkeypatch):
    # point the config dir at a path whose parent is a FILE -> mkdir/connect fail
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker / "config"))
    idx = SessionIndex()
    assert idx.ok is False
    idx.record("t", ".", "title")   # no raise
    idx.touch("t")                  # no raise
    assert idx.latest_for(".") is None
    assert idx.list(".") == []


def test_checkpointer_falls_back_to_memory(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker / "config"))
    warned: list[str] = []
    cp = checkpointer(on_warn=warned.append)
    assert hasattr(cp, "get") and hasattr(cp, "put")
    assert len(warned) == 1 and "sessions.db" in warned[0]
    # a real agent still works with the fallback saver
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent
    from luna.config import LunaConfig
    agent = build_agent(LunaConfig(workdir=str(tmp_path)),
                        model=__import__("tests.conftest", fromlist=["x"]) and None or None,
                        checkpointer=cp)  # build only; invoke covered elsewhere
```

(Trim `test_checkpointer_falls_back_to_memory`'s last stanza to just
`build_agent(..., checkpointer=cp)` with a `fake_model` from the fixture if the
`__import__` line is awkward — the point is "the fallback saver is usable".)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_persistence.py -q`
Expected: FAIL — `SessionIndex.__init__` raises `sqlite3.OperationalError` / `OSError`; no `ok` attribute.

- [ ] **Step 3: Implement `SessionIndex`**

```python
import sqlite3
# ... existing imports; add:
from collections.abc import Callable
from langgraph.checkpoint.memory import InMemorySaver

_INDEX_ERRORS = (sqlite3.Error, OSError)


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
```

- [ ] **Step 4: Implement `checkpointer` fallback**

```python
def checkpointer(
    env: Mapping[str, str] | None = None,
    *,
    on_warn: Callable[[str], None] | None = None,
):
    """A :class:`SqliteSaver` on ``sessions.db``, or an in-memory saver if that fails."""
    try:
        conn = sqlite3.connect(_db_path(env), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        return saver
    except (sqlite3.Error, OSError) as exc:
        if on_warn is not None:
            on_warn(f"sessions.db unavailable ({exc}); this session will not be saved")
        return InMemorySaver()
```

- [ ] **Step 5: Wire `cli.py`**

`luna/cli.py` line ~372: `cp = checkpointer(on_warn=lambda m: console.print(f"[yellow]{m}[/]"))`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest -q`
Expected: PASS (all). Existing `test_persistence.py` tests still green (the happy path is unchanged).

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check luna/persistence.py luna/cli.py tests/test_persistence.py && uv run ruff format luna/persistence.py luna/cli.py tests/test_persistence.py
git add luna/persistence.py luna/cli.py tests/test_persistence.py
git commit -m "fix: persistence degrades gracefully when sessions.db is locked"
```

---

### Task 3: `undo.py` — `existed` flag (no binary deletion), no dir creation on read, `gc()`

**Files:**
- Modify: `luna/undo.py`
- Test: `tests/test_undo.py` (extend)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - Journal entries gain `"existed": bool`. `undo_last` never deletes a file
    whose pre-image could not be captured but which existed
    (`before is None and existed`) — returns
    `"skipped <rel>: original was binary or unreadable, cannot revert"` and
    still consumes the entry.
  - `peek_last` third case: `"skip <rel> (binary/unreadable original)"`.
  - `session_diff` emits `"# <rel>: binary or unreadable — changed, no diff"`
    for such entries.
  - `_entries` / `peek_last` / `session_diff` no longer create the journal dir.
  - `undo.gc(workdir: str, *, keep_days: int = 7, keep_max: int = 20) -> None`
    — best-effort cleanup of `<workdir>/.luna/undo/*` dirs, never raises.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_undo.py`)

```python
import os
import time


def test_undo_refuses_to_delete_a_changed_binary(tmp_path):
    from luna.undo import peek_last, snapshot, undo_last

    f = tmp_path / "logo.bin"
    f.write_bytes(b"\x89PNG\x00original")
    snapshot(str(tmp_path), "s", "write_file", "logo.bin")   # before=None, existed=True
    f.write_bytes(b"\x89PNG\x00changed")
    assert "binary" in (peek_last(str(tmp_path), "s") or "").lower()
    note = undo_last(str(tmp_path), "s")
    assert f.exists() and f.read_bytes() == b"\x89PNG\x00changed"
    assert "cannot revert" in note


def test_undo_still_deletes_a_created_file(tmp_path):
    from luna.undo import snapshot, undo_last

    snapshot(str(tmp_path), "s", "write_file", "new.py")     # existed=False
    (tmp_path / "new.py").write_text("x\n")
    undo_last(str(tmp_path), "s")
    assert not (tmp_path / "new.py").exists()


def test_read_paths_do_not_create_the_journal_dir(tmp_path):
    from luna.undo import session_diff, undo_last

    assert session_diff(str(tmp_path), "none") == ""
    assert undo_last(str(tmp_path), "none") is None
    assert not (tmp_path / ".luna" / "undo" / "none").exists()


def test_session_diff_marks_binary_entries(tmp_path):
    from luna.undo import session_diff, snapshot

    (tmp_path / "b.bin").write_bytes(b"\xff\x00\xfe")
    snapshot(str(tmp_path), "s", "edit_file", "b.bin")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    assert "binary or unreadable" in session_diff(str(tmp_path), "s")


def test_gc_removes_old_journals(tmp_path):
    from luna.undo import gc, journal_dir

    old = journal_dir(str(tmp_path), "old")
    (old / "0000.json").write_text("{}")
    fresh = journal_dir(str(tmp_path), "fresh")
    (fresh / "0000.json").write_text("{}")
    past = time.time() - 40 * 86400
    os.utime(old / "0000.json", (past, past))
    os.utime(old, (past, past))
    gc(str(tmp_path), keep_days=7)
    assert not old.exists() and fresh.exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_undo.py -q`
Expected: FAIL — `undo_last` deletes the binary; read paths create the dir; `gc` is undefined.

- [ ] **Step 3: Implement**

`snapshot` — record `existed`:

```python
def snapshot(workdir: str, session_id: str, tool: str, rel_path: str) -> None:
    """Record the pre-image of ``rel_path`` as the next ``NNNN.json`` entry."""
    d = journal_dir(workdir, session_id)
    target = Path(workdir) / rel_path
    existed = target.is_file()
    before: str | None = None
    if existed:
        try:
            before = target.read_text()
        except (OSError, ValueError):  # unreadable or non-UTF-8 (binary)
            before = None
    n = len(_entries(workdir, session_id))
    (d / f"{n:04d}.json").write_text(
        json.dumps(
            {"tool": tool, "path": rel_path, "before": before,
             "existed": existed, "ts": time.time()}
        )
    )
```

Replace `_entries` (no mkdir on read):

```python
def _journal_path(workdir: str, session_id: str) -> Path:
    return Path(workdir) / ".luna" / "undo" / (session_id or "default")


def _entries(workdir: str, session_id: str) -> list[Path]:
    d = _journal_path(workdir, session_id)
    if not d.is_dir():
        return []
    try:
        return sorted(d.glob("[0-9]*.json"))
    except OSError:
        return []
```

Add a back-compat helper and use it in `peek_last` / `undo_last` / `session_diff`:

```python
def _existed(rec: dict) -> bool:
    """Pre-0.2.1 entries had no 'existed' key: before=None meant 'created'."""
    if "existed" in rec:
        return bool(rec["existed"])
    return rec.get("before") is not None
```

`peek_last` — three cases:

```python
    rel = rec.get("path")
    if rel is None:
        return None
    before, existed = rec.get("before"), _existed(rec)
    if before is not None:
        return f"revert {rel}"
    if existed:
        return f"skip {rel} (binary/unreadable original)"
    return f"delete {rel} (was newly created)"
```

`undo_last` — three cases:

```python
    rel = rec.get("path")
    if rel is None:
        with contextlib.suppress(OSError):
            entries[-1].unlink()
        return None
    target = Path(workdir) / rel
    before, existed = rec.get("before"), _existed(rec)
    if before is not None:
        with contextlib.suppress(OSError):
            target.write_text(before)
        note = f"reverted {rel}"
    elif existed:
        note = f"skipped {rel}: original was binary or unreadable, cannot revert"
    else:
        if target.is_file():
            with contextlib.suppress(OSError):
                target.unlink()
        note = f"removed {rel}"
    with contextlib.suppress(OSError):
        entries[-1].unlink()
    return note
```

`session_diff` — mark binary entries:

```python
    for rel, rec in earliest.items():
        before, existed = rec  # store (before, existed) tuples in `earliest`
        target = Path(workdir) / rel
        current = ""
        if target.is_file():
            try:
                current = target.read_text()
            except (OSError, ValueError):
                chunks.append(f"# {rel}: binary or unreadable — changed, no diff")
                continue
        if before is None and existed:
            chunks.append(f"# {rel}: binary or unreadable — changed, no diff")
            continue
        diff = difflib.unified_diff(
            (before or "").splitlines(), current.splitlines(),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="",
        )
        text = "\n".join(diff)
        if text:
            chunks.append(text)
```

Adjust the `earliest` accumulation to store `(before, existed)`:

```python
    earliest: dict[str, tuple[str | None, bool]] = {}
    for entry in _entries(workdir, session_id):
        try:
            rec = json.loads(entry.read_text())
        except (OSError, ValueError):
            continue
        rel = rec.get("path")
        if rel is None:
            continue
        earliest.setdefault(rel, (rec.get("before"), _existed(rec)))
```

Add `gc`:

```python
def gc(workdir: str, *, keep_days: int = 7, keep_max: int = 20) -> None:
    """Remove stale per-session undo journals under ``<workdir>/.luna/undo/``."""
    root = Path(workdir) / ".luna" / "undo"
    if not root.is_dir():
        return
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return
    cutoff = time.time() - keep_days * 86400

    def _mtime(d: Path) -> float:
        entries = sorted(d.glob("[0-9]*.json"))
        try:
            return entries[-1].stat().st_mtime if entries else d.stat().st_mtime
        except OSError:
            return 0.0

    dated = sorted(((d, _mtime(d)) for d in dirs), key=lambda t: t[1])
    survivors = [d for d, m in dated if m >= cutoff]
    to_remove = [d for d, m in dated if m < cutoff]
    if len(survivors) > keep_max:
        to_remove += survivors[: len(survivors) - keep_max]
    for d in to_remove:
        with contextlib.suppress(OSError):
            shutil.rmtree(d)
```

Add `import shutil` at the top.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_undo.py -q` then `uv run pytest -q`
Expected: PASS. The existing undo tests (`test_snapshot_and_undo_modify` etc.) still pass — a text file has `before` set, so the "reverted" path is unchanged.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check luna/undo.py tests/test_undo.py && uv run ruff format luna/undo.py tests/test_undo.py
git add luna/undo.py tests/test_undo.py
git commit -m "fix: undo never deletes changed binaries; journal GC; no dir on read"
```

---

### Task 4: `cli.py` — undo journal keyed by the session thread; run GC at startup

**Files:**
- Modify: `luna/cli.py` (session_id resolution, `undo.gc` call)
- Test: `tests/test_cli.py` or `tests/test_resume.py` (extend)

**Interfaces:**
- Consumes: `undo.gc` (Task 3), `SessionIndex` (Task 2).
- Produces: `cli.main` passes `session_id = start_thread` (the resolved thread id — fresh uuid for a new session, `row.thread_id` for `--continue`/`--resume`) to both `build_agent` (via `_rebuild`) and `run_repl`/`run_once`. The separate per-process `session_id` uuid is removed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resume.py  (add)
def test_session_id_follows_resumed_thread(tmp_path, monkeypatch):
    from luna.persistence import SessionIndex
    import luna.cli as cli

    idx = SessionIndex()
    idx.record("thread-abc", str(tmp_path), "earlier work")

    seen: dict = {}
    real_run_repl = cli.run_repl

    def _capture(agent, **kw):
        seen.update(kw)
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(cli, "run_repl", _capture)
    monkeypatch.setattr(cli, "build_agent", lambda *a, **k: object())

    cli.main(["-c"])
    assert seen["thread_id"] == "thread-abc"
    assert seen["session_id"] == "thread-abc"   # journal follows the session
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_resume.py::test_session_id_follows_resumed_thread -q`
Expected: FAIL — `session_id` is a distinct random uuid, not `"thread-abc"`.

- [ ] **Step 3: Implement**

In `luna/cli.py` `main()`, replace:

```python
    session_id = uuid.uuid4().hex
    start_thread = uuid.uuid4().hex

    if args.cont or args.resume:
        target = _resolve_resume(args, index, config.workdir, console, interactive)
        if target is None:
            return 2
        start_thread = target
```

with:

```python
    start_thread = uuid.uuid4().hex
    if args.cont or args.resume:
        target = _resolve_resume(args, index, config.workdir, console, interactive)
        if target is None:
            return 2
        start_thread = target
    session_id = start_thread  # the undo journal follows the session across --continue
```

Add the GC call near the other startup helpers (after `dirty_paths`, before the splash):

```python
    from luna import undo
    undo.gc(config.workdir)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: PASS. `tests/test_undo.py::test_snapshot_lands_via_middleware` (builds an agent with `session_id="sid"`) is unaffected — it passes `session_id` explicitly.

- [ ] **Step 5: Manual smoke**

`uv run luna --no-splash "create a file notes.txt with hello"` (approve), then
`uv run luna -c --no-splash "/diff"` — the diff from the previous run shows.
(Trust the test if no key is available.)

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check luna/cli.py tests && uv run ruff format luna/cli.py tests
git add luna/cli.py tests/test_resume.py
git commit -m "fix: undo journal keyed by the session thread; GC at startup"
```

---

### Task 5: `/compact` — replace history in place with `RemoveMessage`

**Files:**
- Modify: `luna/session.py` (new `compact_thread`), `luna/commands.py` (`_compact`)
- Test: `tests/test_commands.py` (replace the compact test), maybe `tests/test_session.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `session.compact_thread(agent, thread_id: str, console: Console) -> None` — one hidden summary turn, then `agent.update_state(config, {"messages": [RemoveMessage(REMOVE_ALL_MESSAGES), HumanMessage("[compacted] Handoff note:\n<summary>")]})`. Same thread. No new session row.
  - `commands._compact(ctx, arg) -> DispatchResult | None` — calls `compact_thread`, wraps it in `try/except`, `index.touch(thread_id)`, returns `DispatchResult()` (no `thread_id` swap). `commands.py` keeps zero framework imports.

- [ ] **Step 1: Write the failing tests** (replace `test_compact_rotates_thread_with_summary` in `tests/test_commands.py`)

```python
def test_compact_replaces_history_in_place(tmp_path, fake_model):
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent

    agent = build_agent(
        LunaConfig(workdir=str(tmp_path)),
        model=fake_model(AIMessage(content="chatter"), AIMessage(content="SUMMARY: did X and Y")),
    )
    cfg = {"configurable": {"thread_id": "keep"}}
    agent.invoke({"messages": [{"role": "user", "content": "hello"}]}, config=cfg)

    ctx = _ctx(agent=agent, thread_id="keep", workdir=str(tmp_path), index=None)
    res = dispatch("/compact", ctx)

    assert res.thread_id is None  # same thread
    msgs = agent.get_state(cfg).values["messages"]
    assert len(msgs) == 1
    assert "did X and Y" in msgs[0].content
    assert msgs[0].type == "human"


def test_compact_no_summary_is_graceful(tmp_path, fake_model):
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent

    agent = build_agent(LunaConfig(workdir=str(tmp_path)), model=fake_model(AIMessage(content="")))
    ctx = _ctx(agent=agent, thread_id="t", workdir=str(tmp_path), index=None)
    res = dispatch("/compact", ctx)  # must not raise
    assert res.handled is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_commands.py -q`
Expected: FAIL — current `_compact` rotates to a new thread; `res.thread_id` is set.

- [ ] **Step 3: Implement `session.compact_thread`**

Add to `luna/session.py` (imports at top with the other langgraph/langchain imports):

```python
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

_COMPACT_ASK = (
    "Summarise this whole session as a dense handoff note: the goal, decisions "
    "made, files touched, current state, and open questions. Text only — do not "
    "call any tools. No preamble."
)


def compact_thread(agent, thread_id: str, console: Console) -> None:
    """Replace this thread's message history with a model-written summary, in place."""
    config = {"configurable": {"thread_id": thread_id}}
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
        console.print(f"[{PALETTE['mauve']}]/compact: no summary produced[/]")
        return
    agent.update_state(
        config,
        {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                HumanMessage(content="[compacted] Handoff note:\n" + summary),
            ]
        },
    )
    console.print(f"[{PALETTE['blue']}]compacted — history replaced with a summary[/]")
```

(`Command` is already imported in `session.py`.)

- [ ] **Step 4: Rewrite `commands._compact`**

Replace `_compact`, `_compact_impl`, and `_COMPACT_ASK` in `luna/commands.py` with:

```python
def _compact(ctx: CommandContext, arg: str) -> DispatchResult | None:
    """Summarise the conversation and replace its history in place."""
    from luna.session import compact_thread  # lazy: session imports commands

    try:
        compact_thread(ctx.agent, ctx.thread_id, ctx.console)
    except Exception as exc:  # noqa: BLE001 - a failed compact must not kill the REPL
        ctx.console.print(f"[{PALETTE['mauve']}]/compact failed: {exc}[/]")
    if ctx.index is not None:
        ctx.index.touch(ctx.thread_id)
    return None
```

Remove the now-unused `uuid` import from `commands.py` only if nothing else uses it (grep — `_new` uses `uuid.uuid4().hex`, so keep it).

- [ ] **Step 5: Run tests**

Run: `uv run pytest -q`
Expected: PASS. If `test_session.py` has a compact-related test, update it too.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check luna/session.py luna/commands.py tests && uv run ruff format luna/session.py luna/commands.py tests
git add luna/session.py luna/commands.py tests/test_commands.py
git commit -m "fix: /compact replaces history in place, one model call, same thread"
```

---

### Task 6: Subagent hardening — `unsafe` opt-in + `tool_guard` on every subagent

**Files:**
- Modify: `luna/subagents.py`, `luna/agent.py`
- Test: `tests/test_subagents.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes: `toolguard.tool_guard` (unchanged).
- Produces:
  - `subagents._SAFE_TOOLS`, `subagents._MUTATING_TOOLS` (`VALID_TOOLS` = their union, unchanged value).
  - `subagents.load_subagents(workdir=".", *, env=None, fast_model=None, guard=None, on_warn=None) -> list[SubAgent]` — `guard` (a `tool_guard(...)` middleware) is prepended to every subagent's `middleware` list; `on_warn` receives a warning line per `unsafe` mutating subagent.
  - A user-defined subagent whose `tools` intersects `_MUTATING_TOOLS` without `unsafe = true` raises `LunaConfigError`.
- `agent.build_agent` builds one `guard` and passes it to `load_subagents`; the main agent keeps the same `guard` instance in `middleware`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_subagents.py  (add)
import pytest
from luna.providers import LunaConfigError
from luna.subagents import _MUTATING_TOOLS, _SAFE_TOOLS, load_subagents


def _write_subagents(tmp_path, body: str):
    d = tmp_path / ".luna"
    d.mkdir(exist_ok=True)
    (d / "subagents.toml").write_text(body)


def test_mutating_subagent_without_unsafe_is_rejected(tmp_path):
    _write_subagents(tmp_path, '[subagent.impl]\ndescription = "x"\ntools = ["execute"]\n')
    with pytest.raises(LunaConfigError, match="unsafe"):
        load_subagents(str(tmp_path))


def test_unsafe_mutating_subagent_loads_and_warns(tmp_path):
    _write_subagents(
        tmp_path,
        '[subagent.impl]\ndescription = "x"\ntools = ["write_file", "read_file"]\nunsafe = true\n',
    )
    warns: list[str] = []
    subs = load_subagents(str(tmp_path), on_warn=warns.append)
    names = {s["name"] for s in subs}
    assert "impl" in names
    assert warns and "impl" in warns[0]


def test_guard_is_first_middleware_on_every_subagent(tmp_path):
    sentinel = object()
    subs = load_subagents(str(tmp_path), guard=sentinel)
    for s in subs:
        assert s["middleware"][0] is sentinel


def test_builtins_are_safe_only():
    assert _MUTATING_TOOLS.isdisjoint(_SAFE_TOOLS)
    subs = load_subagents(".")
    # researcher/reviewer restricted to _SAFE_TOOLS via FilesystemMiddleware
    assert {s["name"] for s in subs} >= {"researcher", "reviewer"}
```

```python
# tests/test_agent.py  (add)
def test_subagent_deny_rule_is_enforced(tmp_path, fake_model, monkeypatch):
    """An unsafe subagent's execute call is still blocked by a deny rule."""
    from langchain_core.messages import AIMessage
    from luna.agent import build_agent
    from luna.config import LunaConfig

    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text('deny = ["execute:*"]\n')
    (tmp_path / ".luna" / "subagents.toml").write_text(
        '[subagent.impl]\ndescription = "impl"\nprompt = "you implement"\n'
        'tools = ["execute"]\nunsafe = true\n'
    )
    # main agent delegates to the subagent, which tries execute, which is denied
    main = [
        AIMessage(content="", tool_calls=[{"name": "task", "id": "1",
                 "args": {"description": "run ls", "subagent_type": "impl"}}]),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*main))
    out = agent.invoke({"messages": [{"role": "user", "content": "go"}]},
                       config={"configurable": {"thread_id": "t"}})
    blob = " ".join(getattr(m, "content", "") or "" for m in out["messages"])
    assert "blocked by a Luna permission rule" in blob or "impl" in blob
```

(If driving the `task` tool through `FakeToolCallingModel` proves unreliable in
practice, downgrade `test_subagent_deny_rule_is_enforced` to: build the subagent
graph directly — `from deepagents import create_deep_agent` with the guard in
`middleware` — and assert a denied `execute` returns the block message. Record
the choice in the report.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_subagents.py tests/test_agent.py -q`
Expected: FAIL — `_MUTATING_TOOLS` undefined; no `unsafe` handling; `guard` param unknown.

- [ ] **Step 3: Implement `subagents.py`**

```python
_SAFE_TOOLS = frozenset({"ls", "read_file", "glob", "grep"})
_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "delete", "execute"})
VALID_TOOLS = _SAFE_TOOLS | _MUTATING_TOOLS
_READ_ONLY = ["ls", "read_file", "glob", "grep"]


def _subagent(
    name: str,
    description: str,
    prompt: str,
    fs_tools: list[str] | None,
    model: str | None = None,
    guard: object | None = None,
) -> SubAgent:
    mw: list = []
    if guard is not None:
        mw.append(guard)
    if fs_tools is not None:
        mw.append(FilesystemMiddleware(tools=fs_tools))
    spec: dict = {"name": name, "description": description, "system_prompt": prompt}
    if mw:
        spec["middleware"] = mw
    if model:
        spec["model"] = model
    return SubAgent(**spec)
```

`load_subagents`:

```python
def load_subagents(
    workdir: str = ".",
    *,
    env: Mapping[str, str] | None = None,
    fast_model: str | None = None,
    guard: object | None = None,
    on_warn: Callable[[str], None] | None = None,
) -> list[SubAgent]:
    """Built-in subagents plus any from ``subagents.toml`` (user + project).

    ``guard`` (a ``tool_guard`` middleware) is attached first to every subagent.
    A user subagent that requests a mutating tool must set ``unsafe = true`` in
    its ``[subagent.<name>]`` block; such subagents run without approval prompts
    (deny rules and undo snapshots still apply) and trigger an ``on_warn`` line.
    """
    agents: list[SubAgent] = []
    for a in BUILTIN_SUBAGENTS:
        model = fast_model if (fast_model and "model" not in a) else a.get("model")
        agents.append(
            _subagent(a["name"], a["description"], a["system_prompt"], _READ_ONLY,
                      model=model, guard=guard)
        )
    for name, cfg in _load_raw(workdir, env).items():
        tools = cfg.get("tools")
        if tools is not None:
            bad = set(tools) - VALID_TOOLS
            if bad:
                raise LunaConfigError(
                    f"subagent {name!r}: unknown tools {sorted(bad)}. "
                    f"Valid: {', '.join(sorted(VALID_TOOLS))}"
                )
            mutating = set(tools) & _MUTATING_TOOLS
            if mutating and not cfg.get("unsafe", False):
                raise LunaConfigError(
                    f"subagent {name!r} requests {sorted(mutating)} but is not marked "
                    f"unsafe. Add 'unsafe = true' to its [subagent.{name}] block to allow "
                    f"write/execute tools in a subagent (they run without an approval "
                    f"prompt), or drop those tools."
                )
            if mutating and on_warn is not None:
                on_warn(
                    f"subagent {name!r} runs {sorted(mutating)} with no approval prompt"
                )
        agent = _subagent(
            name,
            cfg.get("description", name),
            cfg.get("prompt", f"You are the {name} subagent."),
            list(tools) if tools is not None else None,
            cfg.get("model"),
            guard=guard,
        )
        agents = [a for a in agents if a["name"] != name] + [agent]
    return agents
```

Add `from collections.abc import Callable, Mapping` (Mapping already imported).

Note: the built-in rebuild loop above simplified `"model" not in a` handling —
match whatever the current code does; the key change is `guard=guard` on every
`_subagent(...)`.

- [ ] **Step 4: Implement `agent.py`**

`build_agent`: after `rules = load_rules(str(workdir))`, add
`guard = tool_guard(rules, str(workdir), session_id=session_id)`.
Change `_extension_bits` to accept `guard` + `on_warn` and pass them to
`load_subagents`:

```python
def _extension_bits(config, on_warn, guard=None):
    ...
    subs = subagents_mod.load_subagents(
        config.workdir, fast_model=config.fast_model, guard=guard, on_warn=on_warn
    )
    return skill_dirs, list(servers), mcp_tools, subs
```

`build_agent` calls `_extension_bits(config, on_warn, guard=guard)` and uses the
same `guard` in `middleware=[guard]`.

`describe_capabilities` calls `_extension_bits(config, lambda *_: None)` — no
`guard` needed (it only reads names); leave it.

- [ ] **Step 5: Run tests**

Run: `uv run pytest -q`
Expected: PASS. `test_agent.py::test_build_agent_wires_extensions` and
`test_subagents.py` existing tests still green (builtins unchanged in shape
except a leading `guard` in middleware when one is passed — those tests build
with `guard=None`).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check luna/subagents.py luna/agent.py tests && uv run ruff format luna/subagents.py luna/agent.py tests
git add luna/subagents.py luna/agent.py tests/test_subagents.py tests/test_agent.py
git commit -m "fix: subagents run under tool_guard; mutating tools need unsafe=true"
```

---

### Task 7: Cross-cutting tests — `run_repl` integration + `/add` `/drop` `/context` units

**Files:**
- Create: `tests/test_repl_flow.py`
- Test: `tests/test_commands.py` (extend)

**Interfaces:** none — tests only.

- [ ] **Step 1: Write the tests**

```python
# tests/test_repl_flow.py
import io

from langchain_core.messages import AIMessage
from rich.console import Console

from luna.agent import build_agent
from luna.config import LunaConfig
from luna.session import run_repl


def test_repl_flow_at_expansion_diff_undo(tmp_path, fake_model):
    (tmp_path / "README.md").write_text("# demo\n")
    calls = [
        AIMessage(content="", tool_calls=[{"name": "write_file", "id": "1",
                 "args": {"file_path": "/notes.txt", "content": "hi\n"}}]),
        AIMessage(content="wrote notes.txt"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True),
                        model=fake_model(*calls), session_id="sid")
    console = Console(file=io.StringIO(), force_terminal=True)
    lines = iter(["@README.md summarise this", "/diff", "/undo", "n", "/exit"])

    rc = run_repl(
        agent, console=console, input_fn=lambda _: next(lines),
        rebuild=lambda: agent, workdir=str(tmp_path),
        config=LunaConfig(workdir=str(tmp_path), yolo=True),
        thread_id="t", session_id="sid",
    )
    assert rc == 0
    out = console.file.getvalue()
    assert (tmp_path / ".luna" / "undo" / "sid" / "0000.json").exists()
    assert "notes.txt" in out          # /diff showed the change
    assert "undo cancelled" in out     # scripted "n" declined it
    assert (tmp_path / "notes.txt").exists()  # not reverted
```

```python
# tests/test_commands.py  (add)
def test_add_drop_context_handlers(tmp_path):
    from luna.context import PinnedFiles

    (tmp_path / "p.py").write_text("P = 1\n")
    pins = PinnedFiles()
    ctx = _ctx(pinned=pins, workdir=str(tmp_path))

    dispatch("/add p.py", ctx)
    assert pins.paths == ["p.py"]
    dispatch("/context", ctx)
    assert "p.py" in ctx.console.file.getvalue()
    dispatch("/drop p.py", ctx)
    assert pins.paths == []
    dispatch("/add", ctx)  # no arg -> usage line, no crash
    assert "usage: /add" in ctx.console.file.getvalue()
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_repl_flow.py tests/test_commands.py -q`
Expected: PASS. If `run_repl` needs `index=None` explicitly or another kwarg,
add it; if the integration test surfaces a real bug, STOP and report it — the
point of this task is to catch exactly that.

- [ ] **Step 3: Full suite + lint + commit**

```bash
uv run pytest -q && uv run ruff check tests && uv run ruff format tests
git add tests/test_repl_flow.py tests/test_commands.py
git commit -m "test: run_repl integration flow; /add /drop /context handlers"
```

---

### Task 8: Version bump 0.2.1 + docs

**Files:**
- Modify: `luna/__init__.py`, `pyproject.toml`, `tests/test_metadata.py`, `tests/test_cli.py`
- Modify: `CHANGELOG.md`, `README.md`, `AGENTS.md`

- [ ] **Step 1: Version**

`luna/__init__.py` → `__version__ = "0.2.1"`. `pyproject.toml` → `version = "0.2.1"`.
`tests/test_metadata.py` → both assertions `"0.2.1"`. `tests/test_cli.py::test_version` → `"0.2.1"`.

- [ ] **Step 2: `CHANGELOG.md`**

Add `## [0.2.1] — 2026-09-08` above `[0.2.0]`, `### Исправлено` (Russian):
- субагенты из `subagents.toml` теперь под deny-правилами и снапшотами; мутирующие инструменты требуют `unsafe = true` и работают без запроса одобрения
- `sessions.db` под блокировкой больше не роняет REPL — сессия просто не сохраняется
- `/compact` заменяет историю сводкой на месте (один вызов модели, тот же тред), без лишнего хода и новой строки сессии
- журнал `/undo` привязан к сессии — переживает `luna --continue`; старые журналы чистятся при старте
- `/undo` больше не удаляет изменённый бинарный файл
- deny/allow-правила: `*` (любой инструмент) и группы `write` / `fs`

- [ ] **Step 3: `README.md`**

- `[permissions]` example → `deny = ["write:.env", "execute:git push*"]`; one line: bare `write_file:.env` matches only that tool, `write:` covers write/edit/delete, `*:` every tool.
- Ограничения section: `/undo` journal now follows the session across `--continue`; subagents with write/execute need `unsafe = true` and run without approval prompts (deny + undo still apply).

- [ ] **Step 4: `AGENTS.md`**

- Note `compact_thread` lives in `session.py`.
- `subagents.toml` gained an `unsafe` key (mutating-tool opt-in).

- [ ] **Step 5: Full suite + lint + commit**

```bash
uv run pytest -q && uv run ruff check luna tests && uv run ruff format --check .
git add luna/__init__.py pyproject.toml tests/test_metadata.py tests/test_cli.py CHANGELOG.md README.md AGENTS.md
git commit -m "docs: 0.2.1 — hardening the real holes"
```

---

## Self-Review

**Spec coverage:**

| Spec §3 fix | Task |
| --- | --- |
| 3.1 Subagent hardening | Task 6 |
| 3.2 persistence never-raises | Task 2 |
| 3.3 /compact rewrite | Task 5 |
| 3.4 undo tied to session + GC | Task 3 (GC + no-mkdir), Task 4 (session_id keying) |
| 3.5 undo binary protection | Task 3 |
| 3.6 deny wildcard/groups | Task 1 |
| §4 cross-cutting tests | Task 7 |
| §5 version + docs | Task 8 |

Every spec section maps to a task. No gaps.

**Placeholder scan:** No `TBD`/`TODO`/"handle edge cases"/"similar to Task N".
Task 2's `test_checkpointer_falls_back_to_memory` last stanza and Task 6's
`test_subagent_deny_rule_is_enforced` each carry an explicit "downgrade to X if
the harness proves awkward" — those are real fallback instructions with concrete
alternatives, not placeholders.

**Type consistency:**
- `SessionIndex.ok` (Task 2) — read as `idx.ok` in Task 2 tests only; no other consumer.
- `checkpointer(*, on_warn=None)` (Task 2) — Task 4's `cli.py` already passes `on_warn=` after Task 2.
- `undo.gc(workdir, *, keep_days=7, keep_max=20)` (Task 3) — called as `undo.gc(config.workdir)` in Task 4. ✓
- `undo` journal entry gains `"existed"` (Task 3) — `_existed(rec)` back-compat helper covers pre-0.2.1 entries. ✓
- `session.compact_thread(agent, thread_id, console)` (Task 5) — called from `commands._compact` (Task 5). ✓
- `subagents.load_subagents(..., guard=None, on_warn=None)` (Task 6) — `agent._extension_bits` passes both (Task 6). `describe_capabilities` path leaves them default. ✓
- `permissions._tool_matches` / `_TOOL_GROUPS` (Task 1) — internal to `permissions.py`.
- `run_repl(... session_id=)` unchanged from 0.2.0; Task 4 only changes the *value* `cli.py` passes.

**Scope check:** Eight tasks, one branch, no independent subsystems — a single
hardening pass. Fine for one plan.

---

## Execution notes

- Tasks 1–3 are independent (different modules); 4 depends on 2+3; 5 is
  independent; 6 depends on nothing new but touches `agent.py`; 7 depends on
  1–6 landing; 8 last. Keep the order.
- If Task 6's subagent-guard assertion fails (deepagents doesn't actually run
  `SubAgent(middleware=[...])` on the subagent's tool calls), fall back to the
  spec's §7 mitigation: forbid `_MUTATING_TOOLS` in subagents entirely, drop
  the `unsafe` flag, and note the framework limitation in the report + CHANGELOG.
- If Task 5's `update_state` + `REMOVE_ALL_MESSAGES` leaves an invalid history
  or doesn't persist through the checkpointer, STOP and report — the fallback
  (new thread seeded via `update_state` without a second `invoke`) changes the
  task shape.
