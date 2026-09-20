# Luna Anchor-Based Edits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refuse `write_file`/`edit_file`/`delete` calls on a path Luna has
already read or written in this session when the file's on-disk content no
longer matches what Luna last saw — catching stale-context edits (a hand
edit by the user, another `luna` process, or simply outdated model
context) with one process-agnostic mechanism.

**Architecture:** A new pure module, `luna/turn/anchor.py`, tracks a
per-session `dict[str, str]` of relative path → sha256 content hash.
`luna/core/toolguard.py`'s existing `wrap_tool_call` middleware — already
the single place every tool call passes through — gains one new check
(before the existing undo-journal snapshot, after the existing deny/plan
gates) and one new post-call update (after `handler(request)` runs).

**Tech Stack:** Python 3.11+ stdlib only (`hashlib`, `pathlib`) — no new
dependency. Tests use the project's existing `fake_model` fixture
(`FakeToolCallingModel`) and drive real `build_agent(...)` +
`agent.invoke(...)` calls against a real temp directory, matching the
established pattern already used for `tool_guard` in `tests/test_agent.py`
(no lower-level mock of `wrap_tool_call`'s internal `request`/`handler`
shape exists anywhere in this codebase, and this plan does not introduce
one).

**Spec:** `docs/superpowers/specs/2026-09-21-luna-anchor-edits-design.md`

## Global Constraints

- Python 3.11+, PEP 8 / PEP 257, clean `uv run ruff check .` /
  `uv run ruff format --check .`. Docstrings are checked for D401
  (imperative mood) — write "Return X", not "X is returned" or a bare
  noun phrase, for every function/method docstring's first line.
- `luna/turn/anchor.py` must NOT import `deepagents` or `langgraph` —
  it is not one of the four files `AGENTS.md` names as exceptions
  (`luna/core/{agent,session,persistence,toolguard}.py`), and doesn't
  need to: it only touches the filesystem via `pathlib`/`hashlib`.
- Tests never touch the network (not applicable to network directly here,
  but: never mock around the real filesystem or the real `tool_guard`
  logic — every integration test in this plan drives real files in a
  `tmp_path` through a real `build_agent(...)`, matching this project's
  existing convention of testing `toolguard` behavior end-to-end rather
  than unit-testing `wrap_tool_call`'s internal shape).
- `execute` (shell commands) is explicitly out of scope — never anchor-
  check or track it.
- A path with no existing anchor is never blocked — "no anchor" means
  "nothing to be stale against," not "reject unless previously read."

---

### Task 1: `luna/turn/anchor.py` — hashing and per-session tracking

**Files:**
- Create: `luna/turn/anchor.py`
- Test: `tests/test_anchor.py` (new)

**Interfaces:**
- Produces: `luna.turn.anchor.hash_file(workdir: str, rel_path: str) -> str | None`;
  `luna.turn.anchor.AnchorTracker` with methods `remember(workdir: str, rel_path: str) -> None`,
  `check(workdir: str, rel_path: str) -> bool`, `forget(rel_path: str) -> None`.
  Task 2 imports both `AnchorTracker` (constructs one instance per
  `tool_guard(...)` call) and does not call `hash_file` directly — it only
  goes through `AnchorTracker`'s methods.

This task is fully self-contained: no `deepagents` involved, no agent
build, just plain filesystem operations against a `tmp_path`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_anchor.py`:

```python
from luna.turn import anchor


def test_hash_file_returns_none_for_a_missing_file(tmp_path):
    assert anchor.hash_file(str(tmp_path), "nope.txt") is None


def test_hash_file_returns_the_same_hash_for_unchanged_content(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    first = anchor.hash_file(str(tmp_path), "a.txt")
    second = anchor.hash_file(str(tmp_path), "a.txt")
    assert first is not None
    assert first == second


def test_hash_file_changes_when_content_changes(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    before = anchor.hash_file(str(tmp_path), "a.txt")
    (tmp_path / "a.txt").write_text("world\n")
    after = anchor.hash_file(str(tmp_path), "a.txt")
    assert before != after


def test_tracker_check_is_true_when_there_is_no_anchor_yet(tmp_path):
    tracker = anchor.AnchorTracker()
    assert tracker.check(str(tmp_path), "never-read.txt") is True


def test_tracker_check_is_true_when_content_is_unchanged_since_remember(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    tracker = anchor.AnchorTracker()
    tracker.remember(str(tmp_path), "a.txt")
    assert tracker.check(str(tmp_path), "a.txt") is True


def test_tracker_check_is_false_when_content_changed_since_remember(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    tracker = anchor.AnchorTracker()
    tracker.remember(str(tmp_path), "a.txt")
    (tmp_path / "a.txt").write_text("changed\n")
    assert tracker.check(str(tmp_path), "a.txt") is False


def test_tracker_check_is_false_when_the_anchored_file_was_deleted(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    tracker = anchor.AnchorTracker()
    tracker.remember(str(tmp_path), "a.txt")
    (tmp_path / "a.txt").unlink()
    assert tracker.check(str(tmp_path), "a.txt") is False


def test_tracker_forget_clears_the_anchor(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    tracker = anchor.AnchorTracker()
    tracker.remember(str(tmp_path), "a.txt")
    tracker.forget("a.txt")
    (tmp_path / "a.txt").write_text("changed\n")
    # forgotten -> no anchor -> nothing to be stale against -> True again
    assert tracker.check(str(tmp_path), "a.txt") is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_anchor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna.turn.anchor'`.

- [ ] **Step 3: Create `luna/turn/anchor.py`**

```python
"""Anchor tracking for staleness-checked file edits (roadmap item #2).

``AnchorTracker`` records, per session, the content hash Luna last saw for
each path it has read or written. Before a mutating call on a path with
an existing anchor, ``luna/core/toolguard.py`` checks whether the file
still matches — catching edits based on stale context (a hand edit by the
user, another ``luna`` process, or simply model context that outlived the
file's actual state) with one mechanism, regardless of what changed the
file. A path with no anchor yet is never blocked: there's nothing to be
stale against.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def hash_file(workdir: str, rel_path: str) -> str | None:
    """Return the sha256 hex digest of the file's current bytes, or None if it can't be read."""
    try:
        return hashlib.sha256((Path(workdir) / rel_path).read_bytes()).hexdigest()
    except OSError:
        return None


class AnchorTracker:
    """Per-session record of the last-seen content hash for tracked paths."""

    def __init__(self) -> None:
        self._anchors: dict[str, str] = {}

    def remember(self, workdir: str, rel_path: str) -> None:
        """Record the file's current content hash as the new anchor."""
        digest = hash_file(workdir, rel_path)
        if digest is not None:
            self._anchors[rel_path] = digest

    def check(self, workdir: str, rel_path: str) -> bool:
        """Return True if there's no anchor yet, or the anchor still matches disk."""
        anchored = self._anchors.get(rel_path)
        if anchored is None:
            return True
        return hash_file(workdir, rel_path) == anchored

    def forget(self, rel_path: str) -> None:
        """Drop the anchor for a deleted path."""
        self._anchors.pop(rel_path, None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_anchor.py -v`
Expected: all 7 pass.

- [ ] **Step 5: Lint**

Run: `uv run ruff check luna/turn/anchor.py tests/test_anchor.py && uv run ruff format --check luna/turn/anchor.py tests/test_anchor.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add luna/turn/anchor.py tests/test_anchor.py
git commit -m "feat: AnchorTracker — per-session content-hash tracking for staleness checks"
```

---

### Task 2: Wire `AnchorTracker` into `toolguard.py`

**Files:**
- Modify: `luna/core/toolguard.py` (whole file — small, shown in full below)
- Modify: `tests/test_agent.py` (this file already contains every existing
  `tool_guard`-through-`build_agent` test, e.g. `test_plan_mode_blocks_mutating_tools`
  and `test_subagent_deny_rule_is_enforced` — add the new tests here,
  matching that established convention rather than creating a new file)

**Interfaces:**
- Consumes: `luna.turn.anchor.AnchorTracker` (Task 1) — `tool_guard(...)`
  constructs exactly one instance, captured by the returned `_guard`
  closure, living exactly as long as that middleware instance (one built
  agent / one session; a `/reload` rebuilds the agent and so naturally
  gets a fresh tracker — safe, since "no anchor yet" is never a block).
- Produces: no new public interface — `tool_guard(...)`'s signature and
  return type are unchanged.

**Verified facts this task depends on** (checked empirically while writing
this plan against the real, unmodified codebase — not guessed):

- `ToolMessage`'s default `status` is the string `"success"` (checked
  directly: `ToolMessage(content="ok", tool_call_id="1").status ==
  "success"`), and `luna/core/session.py` already uses the pattern
  `getattr(msg, "status", "success") != "error"` to detect a failed tool
  call — this task reuses that exact pattern.
- `read_file`'s built-in deepagents tool returns `status="error"` with
  content `"Error: File '<path>' not found"` for a missing file (checked
  directly against a real `build_agent(...)` + `agent.invoke(...)`).
- `edit_file`'s args are `file_path`, `old_string`, `new_string`;
  `write_file`'s are `file_path`, `content`; `delete`'s is `file_path`
  (all three checked directly — matches the existing `file_path`/`path`
  extraction already in `toolguard.py`).
- **The actual gap this task closes, confirmed to exist today**: deepagents'
  built-in `edit_file` only fails when `old_string` no longer appears in
  the file at all (a plain string-match check) — it has no awareness of
  whether the *rest* of the file changed. Reproduced directly: a file
  read, then externally modified in a way that leaves the matched
  substring intact but changes other content, then edited by Luna today
  (on the unmodified codebase) — the edit silently succeeds, applying a
  patch based on stale context with zero warning. `write_file` (a full
  overwrite) has no staleness protection at all today, built-in or
  otherwise. Task 2's tests include a regression test for exactly this
  scenario.
- The two-`invoke()`-calls-on-the-same-thread pattern (call `agent.invoke(...)`
  once, modify a file on disk directly via the test's own `Path.write_text(...)`,
  then call `agent.invoke(...)` again on the *same* `agent` object and
  the *same* `thread_id`) correctly resumes the same `FakeToolCallingModel`'s
  scripted response queue and reuses the same `tool_guard` middleware
  instance (hence the same `AnchorTracker`) — confirmed by running it
  directly against the real codebase before writing this plan. This is
  how every "external modification between read and edit" test below
  simulates the desync scenario without needing to interleave inside a
  single `stream()` call.
- `out["messages"]` from `agent.invoke(...)` contains the **full**
  accumulated thread history, not just messages from that one call — the
  existing `test_plan_mode_blocks_mutating_tools` test already relies on
  this (`blob = " ".join(...)` over the whole list). Tests below do the
  same; checking a specific string appears anywhere in the full blob is
  safe here since only the newly-blocked call introduces that string.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agent.py` (this file already imports `AIMessage`,
`LunaConfig`, `build_agent` at the top — no new imports needed):

```python
def test_edit_after_unchanged_read_succeeds(tmp_path, fake_model):
    (tmp_path / "a.py").write_text("original\n")
    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "id": "1", "args": {"file_path": "/a.py"}}],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "id": "2",
                    "args": {"file_path": "/a.py", "old_string": "original", "new_string": "changed"},
                }
            ],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        config={"configurable": {"thread_id": "t"}},
    )
    assert (tmp_path / "a.py").read_text() == "changed\n"


def test_edit_on_a_never_read_path_succeeds(tmp_path, fake_model):
    """No anchor yet -> nothing to be stale against -> the edit proceeds."""
    (tmp_path / "a.py").write_text("original\n")
    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "id": "1",
                    "args": {"file_path": "/a.py", "old_string": "original", "new_string": "changed"},
                }
            ],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        config={"configurable": {"thread_id": "t"}},
    )
    assert (tmp_path / "a.py").read_text() == "changed\n"


def test_edit_blocked_after_external_change_that_preserves_the_matched_string(tmp_path, fake_model):
    """Regression test for the real gap: deepagents' own edit_file only
    fails when old_string is no longer found at all. An external change
    that keeps 'original' present but alters other content was applied
    unprotected before this task — confirmed directly against the
    unmodified codebase while writing this plan."""
    p = tmp_path / "a.py"
    p.write_text("original\nsecond line\n")
    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "id": "1", "args": {"file_path": "/a.py"}}],
        ),
        AIMessage(content="read done"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "id": "2",
                    "args": {"file_path": "/a.py", "old_string": "original", "new_string": "changed"},
                }
            ],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    thread = {"configurable": {"thread_id": "t"}}
    agent.invoke({"messages": [{"role": "user", "content": "read it"}]}, config=thread)
    p.write_text("original\nMODIFIED second line\n")
    out = agent.invoke({"messages": [{"role": "user", "content": "now edit it"}]}, config=thread)
    blob = " ".join(getattr(m, "content", "") or "" for m in out["messages"])
    assert "changed on disk" in blob
    assert p.read_text() == "original\nMODIFIED second line\n"


def test_write_blocked_after_external_modification(tmp_path, fake_model):
    p = tmp_path / "a.py"
    p.write_text("original\n")
    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "id": "1", "args": {"file_path": "/a.py"}}],
        ),
        AIMessage(content="read done"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "write_file", "id": "2", "args": {"file_path": "/a.py", "content": "fully replaced\n"}}
            ],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    thread = {"configurable": {"thread_id": "t"}}
    agent.invoke({"messages": [{"role": "user", "content": "read it"}]}, config=thread)
    p.write_text("externally changed\n")
    out = agent.invoke({"messages": [{"role": "user", "content": "now write it"}]}, config=thread)
    blob = " ".join(getattr(m, "content", "") or "" for m in out["messages"])
    assert "changed on disk" in blob
    assert p.read_text() == "externally changed\n"


def test_delete_blocked_after_external_modification(tmp_path, fake_model):
    p = tmp_path / "a.py"
    p.write_text("original\n")
    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "id": "1", "args": {"file_path": "/a.py"}}],
        ),
        AIMessage(content="read done"),
        AIMessage(
            content="",
            tool_calls=[{"name": "delete", "id": "2", "args": {"file_path": "/a.py"}}],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    thread = {"configurable": {"thread_id": "t"}}
    agent.invoke({"messages": [{"role": "user", "content": "read it"}]}, config=thread)
    p.write_text("externally changed\n")
    out = agent.invoke({"messages": [{"role": "user", "content": "now delete it"}]}, config=thread)
    blob = " ".join(getattr(m, "content", "") or "" for m in out["messages"])
    assert "changed on disk" in blob
    assert p.exists()


def test_two_edits_on_the_same_path_without_a_reread_both_succeed(tmp_path, fake_model):
    """Luna's own successful edit re-anchors the file — no forced re-read
    is needed before the next edit to the same path."""
    p = tmp_path / "a.py"
    p.write_text("line one\nline two\n")
    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "id": "1", "args": {"file_path": "/a.py"}}],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "id": "2",
                    "args": {"file_path": "/a.py", "old_string": "line one", "new_string": "first line"},
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "id": "3",
                    "args": {"file_path": "/a.py", "old_string": "line two", "new_string": "second line"},
                }
            ],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        config={"configurable": {"thread_id": "t"}},
    )
    assert p.read_text() == "first line\nsecond line\n"


def test_delete_then_recreate_is_not_blocked_by_a_stale_anchor(tmp_path, fake_model):
    """forget() on a successful delete must actually clear the anchor —
    otherwise writing a brand-new file at the same path would be
    incorrectly blocked by the deleted file's old hash."""
    p = tmp_path / "a.py"
    p.write_text("original\n")
    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "id": "1", "args": {"file_path": "/a.py"}}],
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "delete", "id": "2", "args": {"file_path": "/a.py"}}],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "write_file", "id": "3", "args": {"file_path": "/a.py", "content": "brand new\n"}}
            ],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        config={"configurable": {"thread_id": "t"}},
    )
    assert p.read_text() == "brand new\n"
```

- [ ] **Step 2: Run the new tests to verify the right ones fail**

Run: `uv run pytest tests/test_agent.py -v -k "edit_after_unchanged or never_read_path or blocked_after_external or two_edits_on_the_same_path or delete_then_recreate"`
Expected: `test_edit_after_unchanged_read_succeeds`, `test_edit_on_a_never_read_path_succeeds`,
`test_two_edits_on_the_same_path_without_a_reread_both_succeed`, and
`test_delete_then_recreate_is_not_blocked_by_a_stale_anchor` PASS already
(this task hasn't broken anything that used to work — these scenarios
were never blocked and still shouldn't be). `test_edit_blocked_after_external_change_that_preserves_the_matched_string`,
`test_write_blocked_after_external_modification`, and
`test_delete_blocked_after_external_modification` FAIL — the assertion
`"changed on disk" in blob` finds nothing, because nothing blocks these
calls yet (confirm this matches the "gap confirmed to exist today" note
above — these three are the ones this task actually needs to fix).

- [ ] **Step 3: Rewrite `luna/core/toolguard.py`**

Replace the whole file:

```python
"""wrap_tool_call middleware: enforce deny rules and snapshot mutating calls."""

from __future__ import annotations

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from luna.core.permissions import RuleSet
from luna.turn import gitinfo
from luna.turn.anchor import AnchorTracker
from luna.turn.undo import snapshot

_MUTATING = {"write_file", "edit_file", "delete"}
_TRACKED = {"read_file", "write_file", "edit_file", "delete"}


def tool_guard(rules: RuleSet, workdir: str, session_id: str = "", plan=None):
    """Build a ``wrap_tool_call`` middleware that blocks ``deny``-matched calls.

    Non-denied ``write_file`` / ``edit_file`` / ``delete`` calls are snapshotted
    into the per-session undo journal before the tool runs, unless ``workdir``
    is a git repo (then ``undo.begin_turn`` handles snapshots instead). ``plan``
    is an optional ``Callable[[], bool]``; while it returns ``True``, mutating
    calls (``write_file`` / ``edit_file`` / ``delete`` / ``execute``) are refused.
    A ``write_file`` / ``edit_file`` / ``delete`` call on a path this session
    has already read or written is refused if the file's on-disk content no
    longer matches what was last seen — see ``luna.turn.anchor``.
    """
    use_journal = session_id and not gitinfo.is_git_repo(workdir)
    tracker = AnchorTracker()

    @wrap_tool_call
    def _guard(request, handler):
        call = request.tool_call
        name = call.get("name", "")
        args = call.get("args", {}) or {}
        if rules.match(name, args) == "deny":
            return ToolMessage(
                content=f"blocked by a Luna permission rule ({name})",
                tool_call_id=call.get("id", "blocked"),
                status="error",
            )
        if plan is not None and plan() and name in {"write_file", "edit_file", "delete", "execute"}:
            return ToolMessage(
                content=f"plan mode is on — refusing to {name}. Run /plan off to make changes.",
                tool_call_id=call.get("id", "blocked"),
                status="error",
            )
        rel = (args.get("file_path") or args.get("path") or "").lstrip("/")
        if rel and name in _MUTATING and not tracker.check(workdir, rel):
            return ToolMessage(
                content=f"{rel} has changed on disk since Luna last saw it — read it again before editing",
                tool_call_id=call.get("id", "blocked"),
                status="error",
            )
        if use_journal and name in _MUTATING and rel:
            try:
                snapshot(workdir, session_id, name, rel)
            except (OSError, ValueError):
                pass
        result = handler(request)
        if rel and name in _TRACKED and getattr(result, "status", "success") != "error":
            if name == "delete":
                tracker.forget(rel)
            else:
                tracker.remember(workdir, rel)
        return result

    return _guard
```

Note what changed versus the original: `rel` is now computed once, before
the `use_journal` block (previously it was computed only inside that
block) — a pure simplification, same value, no behavior change for the
existing deny/plan/journal logic. The two genuinely new pieces are the
`tracker.check(...)` block (before the journal snapshot) and the
post-`handler` tracking update (after it).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent.py -v`
Expected: all pass, including every pre-existing test in the file (the
`rel`-hoisting refactor must not change `test_subagent_deny_rule_is_enforced`'s
or `test_plan_mode_blocks_mutating_tools`'s behavior).

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -q`
Expected: all pass — this is where any other caller relying on
`toolguard.py`'s exact prior structure would surface a problem.

- [ ] **Step 6: Lint**

Run: `uv run ruff check luna/core/toolguard.py tests/test_agent.py && uv run ruff format --check luna/core/toolguard.py tests/test_agent.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add luna/core/toolguard.py tests/test_agent.py
git commit -m "feat: refuse write_file/edit_file/delete on a path that changed since Luna last saw it"
```

---

### Task 3: Documentation + final verification

**Files:**
- Modify: `AGENTS.md` (the `luna/turn/` file listing in the `## Структура` section)
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing (terminal task).

- [ ] **Step 1: Add `anchor.py` to `AGENTS.md`'s `luna/turn/` listing**

In `AGENTS.md`, find this exact line (currently the last bullet in the
`luna/turn/` list, `## Структура` section):

```markdown
  - `verify.py` — verify-команда
```

Replace it with:

```markdown
  - `verify.py` — verify-команда
  - `anchor.py` — трекинг хешей содержимого прочитанных/записанных
    файлов, staleness-check перед `write_file`/`edit_file`/`delete`
```

- [ ] **Step 2: Add a CHANGELOG entry**

`CHANGELOG.md`'s `## [Unreleased]` section already has a `### Добавлено`
subsection (confirmed while writing this plan — it currently starts with
a bullet about the 8 new providers) — add a new bullet to that existing
subsection, immediately after its current last bullet (the model-picker
one), not a new subsection:

```markdown
- Правки файлов теперь защищены от рассинхрона: если `write_file` /
  `edit_file` / `delete` нацелены на путь, который Luna уже читала или
  писала в этой сессии, а содержимое на диске с тех пор изменилось
  (правка руками, другой процесс `luna`, устаревший контекст хода) —
  вызов отклоняется с понятной просьбой перечитать файл, вместо тихого
  применения к устаревшему содержимому. Собственная правка Luna сама
  становится новым якорем — повторное чтение перед следующей правкой
  того же файла не требуется. `execute` вне охвата этого механизма.
```

- [ ] **Step 3: Run the full verification gate**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all clean — this is the final gate for the whole feature.

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md CHANGELOG.md
git commit -m "docs: document anchor-based edits"
```
