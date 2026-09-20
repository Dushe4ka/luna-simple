# Luna Anchor-Based Edits — Design

## Purpose

Roadmap item #2 (of the 1→2→4→3 order agreed during the competitive-analysis
session): protect Luna's file edits against acting on stale content — the
agent reads a file, forms a plan based on that content, and by the time it
calls `write_file`/`edit_file`/`delete` the file has actually changed
underneath it. Without protection, Luna silently overwrites or patches
against content that no longer exists on disk, destroying whatever changed
it in the meantime.

Three concrete scenarios the user asked this to cover:

1. The user hand-edits the same file in their own editor while Luna is
   mid-turn (between a `read_file` and a later `write_file`/`edit_file` on
   that same path, possibly several tool calls apart).
2. Two separate `luna` processes running against the same repository (two
   terminals) each read and edit the same file.
3. Stale model context within a single turn — the model's understanding of
   a file's content, formed at `read_file` time, no longer matches disk by
   the time it acts on that understanding.

## Research That Shaped This Design

Before designing, the two named competitors were checked directly rather
than assumed:

- **Claude Code** (this tool) already does exactly this, single-session:
  `Edit` refuses to modify a file that changed since the matching `Read`
  ("File has been modified since read, either by the user or by a linter.
  Read it again before attempting to write it."), confirmed via
  `anthropics/claude-code`'s own GitHub issues. This is the proven,
  shipped pattern this design follows.
- **OpenCode** supports genuinely parallel *sessions* but explicitly does
  **not** protect them from clobbering each other's file edits — a file-lock
  mechanism is an open feature request in their own tracker, and the
  project's own documented mitigation is "one git worktree per parallel
  task," i.e. side-stepping the conflict via workspace isolation rather
  than solving it via file-level checks.
- **`deepagents`** (the framework Luna is built on) has a separate
  `quickjs` partner package that supports genuine parallel subagent
  fan-out (`Promise.all` over `task()` calls, capped at 32 concurrent).
  Luna does not depend on this package (confirmed: absent from
  `pyproject.toml` and `uv.lock`) and does not use it — Luna's `task` tool
  is the standard, synchronous one. Adopting `quickjs` would be a
  separate, much larger architectural change (new dependency, new
  subagent execution model) and is explicitly **out of scope** for this
  design. Today, a subagent dispatched via `task` blocks the main agent
  until it finishes, so there is no genuine concurrent file access between
  a subagent and the main agent to protect against yet.

**Conclusion driving the design:** no competitor solves scenario 2
(separate processes) via shared file-content hashing or locking — the one
that ships a working mechanism (Claude Code) solves it with a
single-session "does disk still match what I last read" check, which is
process-agnostic by construction: the check only compares *live disk
content* against *what this session itself last read*, so it does not
matter whether a human, another `luna` process, or anything else changed
the file — any change is caught the same way. This is why a single
mechanism covers all three scenarios without inter-process communication,
lock files, or new dependencies.

## Success Criteria

- Before any `write_file`, `edit_file`, or `delete` call on a path this
  session has previously read, Luna verifies the file's current on-disk
  content still matches what was read — if it doesn't, the call is
  refused with a clear, actionable error telling the agent to re-read the
  file, instead of silently applying a stale edit.
- A path this session has never read is not blocked — there is nothing to
  be stale relative to. This is a deliberate scope boundary: this
  mechanism detects staleness, it does not enforce "always read before
  writing."
- After the agent successfully writes/edits a file itself, that edit
  becomes the new anchor — no forced re-read is needed before the next
  edit to the same path in the same turn or session.
- No new dependency, no lock files, no cross-process communication. The
  mechanism is entirely local to one session's in-memory state plus the
  filesystem.
- `execute` (shell commands) is explicitly out of scope — Luna cannot
  practically anchor-check arbitrary shell-command side effects on
  arbitrary files. This is a documented limitation, not a gap to silently
  leave undocumented.
- Subagents dispatched via `task` are protected automatically, with no
  subagent-specific code, because they run inside the same session under
  the same `toolguard` middleware and therefore share the same tracker
  instance — not because of any explicit parallel-safety mechanism (there
  is no real parallelism to protect against today, see Research above).

## Architecture

### `luna/turn/anchor.py` (new)

Pure logic, no `deepagents`/`langgraph` imports (this file is not one of
the four files `AGENTS.md` permits framework imports in, and doesn't need
to be):

```python
def hash_file(workdir: str, rel_path: str) -> str | None:
    """sha256 of the file's current bytes, or None if it doesn't exist."""
```

```python
class AnchorTracker:
    """Per-session record of 'what did I last see' for read/written paths."""

    def remember(self, workdir: str, rel_path: str) -> None:
        """Record the file's current content hash as the new anchor."""

    def check(self, workdir: str, rel_path: str) -> bool:
        """True if there's no anchor yet, or the anchor still matches disk.
        False means the file changed since this session last saw it."""

    def forget(self, rel_path: str) -> None:
        """Drop the anchor for a deleted path."""
```

`AnchorTracker` holds a plain `dict[str, str]` (relative path → hash)
as instance state — no persistence, no file on disk, lives exactly as
long as the `tool_guard(...)` closure that owns it (i.e., one built
agent / one session; a `/reload` naturally resets it, which is safe: the
next edit on any path simply has no anchor yet, same as a path never
read before).

### `luna/core/toolguard.py` (modified)

`tool_guard(...)` constructs one `AnchorTracker()` alongside its existing
`use_journal` setup. Inside `_guard`, the check sequence becomes:

1. deny-rule check (existing, unchanged)
2. `/plan`-mode check (existing, unchanged)
3. **new:** if `name in {"write_file", "edit_file", "delete"}` and a
   `rel` path is present, call `tracker.check(workdir, rel)` — if `False`,
   return a blocking `ToolMessage` (same shape as the existing deny/plan
   blocks) *before* the undo-journal snapshot step, so a doomed edit never
   consumes an approval prompt or a snapshot.
4. undo-journal snapshot (existing, unchanged)
5. `result = handler(request)` — actually run the tool
6. **new:** on a successful result, update the tracker:
   - `read_file` → `tracker.remember(...)`
   - `write_file` / `edit_file` → `tracker.remember(...)` (the edit Luna
     just made is the new known-good state)
   - `delete` → `tracker.forget(...)`
7. return `result` (unchanged)

Path extraction reuses the exact existing pattern already in this file
(`args.get("file_path") or args.get("path")`, `.lstrip("/")`) for
consistency — not a new convention.

## Data Flow

```
read_file(path)  →  handler runs  →  success  →  tracker.remember(path)

... (any number of other tool calls / turns) ...

edit_file(path, ...)
  → tracker.check(path)?
      unchanged or never-read  → proceed → handler runs → tracker.remember(path)
      changed since last read  → blocked, ToolMessage explains why, agent re-reads
```

## Error Handling

The blocking `ToolMessage` matches the existing deny/plan-mode pattern in
`toolguard.py` exactly (`status="error"`, `tool_call_id` from the call) —
no new exception type, no new response shape. Message text names the file
and tells the agent to re-read it, mirroring Claude Code's own proven
wording rather than inventing new UX.

`hash_file` never raises — matches every other best-effort file-reading
helper already in this codebase (`usage.py`, `model_discovery.py`'s
convention). A file that can't be read (permissions, race with a deletion
mid-check) is treated as `None`, and `check()` against a `None` current
hash is handled explicitly (if the tracked anchor was for a file that
existed and now can't be read/no longer exists, that's a real change —
report `False`, i.e. blocked, same as any other mismatch).

## Testing

`FakeToolCallingModel`-driven, no network, no new dependency. Covers:
read → edit unchanged (passes) · read → external on-disk modification →
edit (blocked, message asserted) · edit on a never-read path (passes,
`check()` returns `True` with no anchor) · edit → edit on the same path
without an intervening read (passes, first edit's `remember()` anchors
the second) · delete after an external modification (blocked) · delete
correctly `forget()`s so a later `write_file` recreating the same path
isn't blocked by a stale anchor from before the deletion.

## Out of Scope (explicitly, not a gap)

- `execute` (shell command) side effects on tracked files.
- Genuine parallel subagent dispatch (would require adopting
  `deepagents`' `quickjs` partner — a separate architectural decision).
- Cross-process lock files / any mechanism requiring processes to
  coordinate directly — the design's whole point is not needing this.
