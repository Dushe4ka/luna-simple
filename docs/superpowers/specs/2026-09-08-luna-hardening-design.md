# Luna — hardening the real holes — Design

**Date:** 2026-09-08
**Status:** Approved (design), pending implementation plan
**Version target:** 0.2.1 (bug fixes + safety; `unsafe` flag and deny groups are additive)
**Follows:** `2026-09-07-luna-competitive-features-design.md` (0.2.0, merged)

## 1. Goal

Close six real defects the 0.2.0 whole-branch review and post-merge analysis
surfaced. Every fix is localised; no new subsystem. Framework imports stay
confined to `agent.py`, `session.py`, `persistence.py`, `toolguard.py`.

The six:

1. **Declarative subagents bypass `tool_guard` and the approval prompt.** A
   subagent defined in `subagents.toml` with `write_file` / `edit_file` /
   `delete` / `execute` runs those with no deny-rule enforcement, no undo
   snapshot, and no human approval — deepagents does not propagate main-agent
   middleware or `interrupt_on` to declarative subagents.
2. **`persistence.py` can crash the REPL.** A locked `sessions.db` (two `luna`
   processes in one directory) raises `sqlite3.OperationalError` out of
   `SessionIndex.record`/`touch` into the top-level `except` in `cli.py`.
3. **`/compact` runs a wasteful second full agent turn**, can hang on a tool
   interrupt on the new thread, and creates a new `luna_sessions` row instead
   of continuing the session.
4. **The undo journal is per-process.** After `luna --continue`, `/diff` and
   `/undo` see nothing from the prior run — inconsistent with the conversation,
   which is durable. Journal directories are also never garbage-collected.
5. **Undo deletes binary files.** A `write_file` over an existing binary file
   snapshots `before=None` (can't decode), and `/undo` then treats it as
   "was newly created" and deletes it.
6. **Deny rules match one exact tool name.** `deny = ["write_file:.env"]` (the
   documented example) leaves `edit_file`, `delete`, and `execute` free to
   touch `.env`.

Out of scope: repo map, sandbox for `execute`, hooks, custom slash commands,
web search, the `/compact` prompt-injection edge beyond a reject-once guard.

## 2. Verified framework facts

- **Declarative subagents (`SubAgent` dicts) do NOT inherit main-agent
  middleware or filesystem restrictions.** `SubAgent` accepts its own
  `middleware: list[Middleware]`. `subagents.py._subagent` already sets
  `spec["middleware"] = [FilesystemMiddleware(tools=...)]` — we prepend
  `tool_guard(...)`.
- **`wrap_tool_call` middleware is an `AgentMiddleware` instance** and can be
  placed in a `SubAgent`'s `middleware` list. It closes over `rules` /
  `workdir` / `session_id` and holds no per-run state, so one instance is
  reusable across the main agent and every subagent.
- **`agent.update_state(config, {"messages": [...]})`** is available on the
  deepagents compiled graph. `RemoveMessage(id=REMOVE_ALL_MESSAGES)` (from
  `langgraph.graph.message`) followed by a new `HumanMessage` replaces the
  whole history in place, because deepagents' `AgentState` uses the
  `add_messages` reducer. Result must remain a valid history (start with a
  user message) — `[HumanMessage(summary)]` satisfies that.
- deepagents does not give declarative subagents `interrupt_on` — approval
  for subagent tool calls is not achievable without a custom HITL middleware
  in the subgraph, which is out of scope. Mutating subagents are therefore
  "trusted-but-guarded": deny + undo apply, approval does not.

Re-verify during implementation: that `SubAgent(middleware=[tool_guard(...)])`
actually intercepts the subagent's tool calls (build one, drive a deny rule,
assert the block); that `update_state` + `REMOVE_ALL_MESSAGES` leaves a valid
history and the checkpointer persists it.

## 3. Fix designs

### 3.1 Subagent hardening (defect 1)

**`luna/subagents.py`:**

- Split the tool constants:
  ```python
  _SAFE_TOOLS = frozenset({"ls", "read_file", "glob", "grep"})
  _MUTATING_TOOLS = frozenset({"write_file", "edit_file", "delete", "execute"})
  VALID_TOOLS = _SAFE_TOOLS | _MUTATING_TOOLS  # unchanged public set
  ```
- `_subagent(name, description, prompt, fs_tools, model=None, guard=None)` —
  when `guard` is not None, `spec["middleware"] = [guard, *fs_mw]` (guard
  first, then `FilesystemMiddleware` if `fs_tools` is set).
- `load_subagents(workdir=".", *, env=None, fast_model=None, guard=None)` —
  thread `guard` into every `_subagent(...)` call (built-in and user).
- Per user-defined subagent: read `unsafe` (bool, default `False`) from its
  `[subagent.<name>]` table. If `tools` intersects `_MUTATING_TOOLS` and
  `unsafe` is not `True`:
  ```
  raise LunaConfigError(
      f"subagent {name!r} requests {sorted(mutating)} but is not marked unsafe. "
      f"Add 'unsafe = true' to its [subagent.{name}] block to allow write/execute "
      f"tools in a subagent (they run without an approval prompt), or drop those tools."
  )
  ```
- When a subagent IS `unsafe` and mutating, emit via `on_warn` (plumbed
  through `load_subagents` -> not currently; add an `on_warn` param defaulting
  to a no-op, called from `_extension_bits` with the real sink):
  ```
  ⚠ subagent 'X' runs write_file/edit_file/delete/execute with no approval prompt
  ```

**`luna/agent.py`:**

- `_extension_bits`: build one `guard = tool_guard(rules, str(workdir), session_id=session_id)`
  and pass `guard=guard, on_warn=on_warn` to `load_subagents`. `rules` /
  `workdir` / `session_id` are already in scope in `build_agent`; move the
  `rules = load_rules(...)` and `guard = ...` construction so `_extension_bits`
  can receive them (pass `rules`, `workdir`, `session_id` as params, or inline
  `_extension_bits` into `build_agent` — implementer's call, keep it readable).
- The main agent keeps `middleware=[guard]` (same instance is fine).
- `describe_capabilities` calls `_extension_bits` — give it a throwaway
  `guard=None` (it only needs names) or a real one; `None` is simplest since
  it never builds a graph.

**`luna/toolguard.py`:** no change needed — the same `tool_guard` factory
serves both. (Optional: a one-line docstring note that it is also attached to
subagents.)

**Tests (`tests/test_subagents.py`):**
- user subagent with `tools = ["execute"]` and no `unsafe` -> `LunaConfigError`
- same with `unsafe = true` -> loads; `load_subagents(..., guard=g)` puts `g`
  first in its `middleware`
- built-in `researcher` still has only `_SAFE_TOOLS` and the guard
- integration: `build_agent` with a project `subagents.toml` (`unsafe` subagent,
  `execute`) + a `deny = ["execute:*"]` rule + a fake model that makes the main
  agent delegate via `task` -> the subagent's `execute` is blocked. (If driving
  `task` through the fake model is too fiddly, fall back to: construct the
  subagent's compiled graph directly via deepagents and assert the guard fires.)

### 3.2 `persistence.py` never raises (defect 2)

**`luna/persistence.py`:**

- `SessionIndex.__init__`: wrap `sqlite3.connect` + `CREATE TABLE` +
  `commit` in `try/except sqlite3.Error/OSError`. On failure: `self._conn =
  None`. Add `self._ok` property (`self._conn is not None`).
- `record` / `touch`: `if self._conn is None: return`. Wrap the `execute` +
  `commit` in `try/except sqlite3.Error: pass` (a mid-session lock must not
  raise either).
- `latest_for` / `list`: `if self._conn is None: return None` / `return []`.
  Wrap the query in `try/except sqlite3.Error: return None/[]`.
- `checkpointer(env=None, *, on_warn=None)`: wrap `connect` + `SqliteSaver` +
  `setup()` in `try/except (sqlite3.Error, OSError) as exc`. On failure:
  `if on_warn: on_warn(f"sessions.db unavailable ({exc}); this session will not be saved")`
  and `return InMemorySaver()` (`from langgraph.checkpoint.memory import InMemorySaver`).

**`luna/cli.py`:** pass `on_warn=lambda m: console.print(f"[yellow]{m}[/]")` to
`checkpointer(...)`.

**Tests (`tests/test_persistence.py`):**
- `SessionIndex` with `XDG_CONFIG_HOME` pointed at a path that cannot be
  created (e.g. a file where the dir should be) -> constructs; `record`,
  `touch` no-op; `latest_for` -> `None`; `list` -> `[]`
- `checkpointer` with the same bad path + a recording `on_warn` -> returns an
  object with `.get`/`.put`, `on_warn` was called once

### 3.3 `/compact` rewrite (defect 3)

**`luna/session.py`** — new helper (langgraph imports live here):

```python
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

_COMPACT_ASK = (
    "Summarise this whole session as a dense handoff note: the goal, decisions "
    "made, files touched, current state, and open questions. Text only — do not "
    "call any tools. No preamble."
)


def compact_thread(agent, thread_id: str, console: Console) -> None:
    """Replace this thread's history with a model-written summary, in place."""
    config = {"configurable": {"thread_id": thread_id}}
    result = agent.invoke({"messages": [{"role": "user", "content": _COMPACT_ASK}]}, config)
    if isinstance(result, dict) and result.get("__interrupt__"):
        # summary turn tried a tool despite instructions — reject once, take what we have
        from langgraph.types import Command
        result = agent.invoke(
            Command(resume={"decisions": [{"type": "reject", "message": "summary only"}]}),
            config,
        )
    summary = ""
    for msg in reversed(result.get("messages", []) if isinstance(result, dict) else []):
        text = getattr(msg, "content", "")
        if getattr(msg, "type", "") == "ai" and isinstance(text, str) and text.strip():
            summary = text.strip()
            break
    if not summary:
        console.print(f"[{PALETTE['mauve']}]/compact: no summary produced[/]")
        return
    agent.update_state(
        config,
        {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES),
                      HumanMessage(content="[compacted] Handoff note:\n" + summary)]},
    )
    console.print(f"[{PALETTE['blue']}]compacted — history replaced with a summary[/]")
```

**`luna/commands.py`:** `_compact` becomes:

```python
def _compact(ctx: CommandContext, arg: str) -> DispatchResult | None:
    from luna.session import compact_thread  # lazy: session imports commands
    try:
        compact_thread(ctx.agent, ctx.thread_id, ctx.console)
    except Exception as exc:  # noqa: BLE001 - a failed compact must not kill the REPL
        ctx.console.print(f"[{PALETTE['mauve']}]/compact failed: {exc}[/]")
    if ctx.index is not None:
        ctx.index.touch(ctx.thread_id)
    return None
```

Delete `_compact_impl`, `_COMPACT_ASK`, the `uuid` use for a new thread, and
the `luna_sessions` row creation from `commands.py`. `commands.py` keeps zero
framework imports.

**Tests (`tests/test_commands.py` / a `compact` test):**
- `/compact` keeps `ctx.thread_id` unchanged (no `res.thread_id`)
- after `/compact`, `agent.get_state(config)["messages"]` is exactly one
  `HumanMessage` whose content contains the fake model's summary text
- no new row in `luna_sessions` (record count unchanged)
- fake model that returns empty content -> "no summary produced", no crash

### 3.4 Undo journal tied to the session + GC (defect 4)

**Journal key = the session's canonical `thread_id`.**

- `luna/cli.py`: `session_id` passed to `build_agent` and `run_repl` becomes
  `start_thread` (the freshly-minted uuid for a new session, or
  `row.thread_id` for `luna --continue` / `--resume`). The separate
  per-process undo uuid is removed. On `--continue`, resuming `row.thread_id`
  gives the same `.luna/undo/<thread_id>/` the earlier run wrote to.
- `/compact` (3.3) keeps the same `thread_id` -> same journal. `/new` rotates
  `thread_id` in `run_repl` but the middleware's baked-in `session_id` stays
  fixed for the process, so `/new` shares the journal within one run (as
  today; acceptable — "fresh conversation", not "fresh repo state").

**`luna/undo.py`:**

- `_entries(workdir, session_id)` and `peek_last` / `session_diff` must NOT
  create the directory. Replace `journal_dir(...).glob(...)` on read paths
  with:
  ```python
  d = Path(workdir) / ".luna" / "undo" / (session_id or "default")
  if not d.is_dir():
      return []
  ```
  `journal_dir` (the `mkdir` version) is used only by `snapshot`.
- New `gc(workdir: str, *, keep_days: int = 7, keep_max: int = 20) -> None`:
  under `<workdir>/.luna/undo/`, for each child dir compute the mtime of its
  newest `*.json` (or the dir mtime if empty); delete dirs older than
  `keep_days`, then if more than `keep_max` remain delete the oldest. All
  wrapped in `try/except OSError: pass` — best effort, never raises.

**`luna/cli.py`:** call `undo.gc(config.workdir)` once at startup (after
config resolution, guarded).

**Tests (`tests/test_undo.py`):**
- `_resolve_resume`-style: a journal written under thread `"t"`, then
  `session_id="t"` threaded through -> `session_diff` / `undo_last` see it
- `gc`: an old dir (mtime backdated) is removed; a fresh one is kept;
  `keep_max` trims the oldest
- `session_diff` on a session with no journal dir does not create it
  (`assert not (tmp/".luna"/"undo"/"none").exists()` after the call)

### 3.5 Undo does not delete binary files (defect 5)

**`luna/undo.py`:**

- `snapshot` records `"existed": target.is_file()` in the entry, alongside
  `before` (which stays `None` when the file is absent OR unreadable/binary).
- `undo_last`:
  - `before` is a `str` -> `target.write_text(before)`, "reverted"
  - `before is None` and `not existed` -> `target.unlink()`, "removed"
  - `before is None` and `existed` -> do nothing; return a note
    `f"skipped {rel}: original was binary or unreadable, cannot revert"`.
    Still consume the journal entry (`entries[-1].unlink()`) so `/undo`
    advances.
- `peek_last`: third case -> `f"skip {rel} (binary/unreadable original)"`.
- `session_diff`: for an entry with `existed and before is None`, emit
  `f"# {rel}: binary or unreadable — changed, no diff"` instead of a
  spurious text diff.
- Old journal entries without an `"existed"` key: treat missing as
  `existed = (before is not None)` for back-compat (a pre-0.2.1 entry with
  `before=None` was always a "created" record).

**Tests (`tests/test_undo.py`):**
- write a non-UTF-8 file, `snapshot`, overwrite it, `undo_last` -> file still
  exists, note mentions "binary or unreadable"
- new file created (`existed=False`), `undo_last` -> deleted (unchanged)
- `session_diff` shows the "binary — no diff" line for the binary entry

### 3.6 Deny rules: wildcard tool + groups (defect 6)

**`luna/permissions.py`:**

```python
_TOOL_GROUPS: dict[str, frozenset[str]] = {
    "write": frozenset({"write_file", "edit_file", "delete"}),
    "fs": frozenset({"write_file", "edit_file", "delete", "read_file", "ls", "glob", "grep"}),
}


def _tool_matches(rule_tool: str, tool: str) -> bool:
    if rule_tool in ("*", tool):
        return True
    group = _TOOL_GROUPS.get(rule_tool)
    return group is not None and tool in group
```

`RuleSet._hit`: replace `if rtool == tool and fnmatch(...)` with
`if _tool_matches(rtool, tool) and fnmatch(normalised_subject, normalised_pattern)`.
Path normalisation (`_relpath`, strip leading `/`) still applies whenever
`tool != "execute"` — the rule's own `rtool` may be `*`/`write`/`fs`, so key
the normalisation off the *actual* `tool`, not `rtool` (already the case;
just confirm).

`suggest_rule` unchanged — `[a] always` still writes a specific
`<exact-tool>:<path>` rule (least surprising).

**Docs:** README `[permissions]` example and CHANGELOG use
`deny = ["write:.env", "execute:git push*"]` and one sentence: a bare
`write_file:.env` matches only that tool; use `write:` for all mutating file
tools or `*:` for every tool.

**Tests (`tests/test_permissions.py`):**
- `RuleSet(deny=["*:.env"])` -> `match("edit_file", {"file_path": "/.env"}) == "deny"`,
  `match("delete", {"file_path": ".env"}) == "deny"`, `match("execute", {"command": "cat .env"})` — `*` matches, pattern `.env` vs `cat .env` -> no match (fine; `*:*env*` would)
- `RuleSet(deny=["write:secrets/*"])` -> blocks `write_file`/`edit_file`/`delete`
  on `secrets/x`, does NOT block `read_file`
- `RuleSet(deny=["fs:x"])` blocks `read_file:x`
- existing exact-name rules still behave identically

## 4. Cross-cutting

- **Integration test for `run_repl`** (`tests/test_session.py` or a new
  `tests/test_repl_flow.py`): a scripted `input_fn` yielding
  `["@README.md what is this", "/diff", "/undo", "n", "/exit"]` and a fake
  model that emits a `write_file` tool call on the first turn. Assert: no
  exception; the `write_file` produced a journal entry; `/undo` prompted (the
  scripted "n" cancelled it); the loop exited 0. Uses a real `build_agent`
  with `yolo=True` and a temp workdir.
- **Handler unit tests** for `/add` / `/drop` / `/context` (deferred minor
  from 0.2.0): `_ctx(pinned=PinnedFiles())`, dispatch, assert `ctx.pinned.paths`
  and the printed output.

## 5. Version + docs

- `luna/__init__.py` -> `0.2.1`; `pyproject.toml` -> `0.2.1`;
  `tests/test_metadata.py` + `tests/test_cli.py::test_version` -> `0.2.1`.
- `CHANGELOG.md`: `## [0.2.1] — 2026-09-08`, `### Исправлено` — one bullet per
  defect, in user terms.
- `README.md`: update the `[permissions]` example; add one line to the
  Ограничения section — undo journal now follows the session across
  `--continue`; subagents with write/execute need `unsafe = true` and run
  without approval prompts.
- `AGENTS.md`: note `compact_thread` lives in `session.py`; `subagents.toml`
  gained an `unsafe` key.

## 6. Implementation order

1. `permissions.py` wildcard/groups + tests (isolated, no deps).
2. `persistence.py` never-raises + `checkpointer` fallback + `cli.py` wiring + tests.
3. `undo.py`: `existed` field + 3-state `undo_last` + no-mkdir-on-read + `gc()` + tests.
4. `cli.py`: `session_id = start_thread`, remove per-process uuid, call `undo.gc` + tests.
5. `session.py` `compact_thread` + `commands.py` `_compact` rewrite + tests.
6. `subagents.py` `unsafe` + guard injection; `agent.py` wiring + tests.
7. Integration test for `run_repl`; `/add`/`/drop`/`/context` handler tests.
8. Version bump + CHANGELOG + README + AGENTS.md.

Each step: its own tests, `ruff check` + `ruff format --check` clean, full
`pytest` green on this machine.

## 7. Risks

| Risk | Mitigation |
| --- | --- |
| `SubAgent(middleware=[tool_guard])` doesn't actually intercept subagent tool calls | Step 6 builds a real subagent graph and asserts a deny rule fires; if it doesn't, fall back to forbidding mutating subagent tools entirely (the user's third option) and note it |
| `update_state` + `REMOVE_ALL_MESSAGES` leaves an invalid history or doesn't persist | Step 5 asserts `get_state` after `/compact` shows exactly the summary message and a follow-up turn works |
| `session_id` = 32-hex `thread_id` as a directory name | fine on all target platforms; `journal_dir` already accepts any string |
| GC deletes a journal a concurrent `luna` is using | `keep_days=7` default is far from any live session; GC only runs at startup |
| Raising `LunaConfigError` for a non-`unsafe` mutating subagent breaks `/reload` mid-session | `/reload`'s `_reload` handler already surfaces the message via the rebuild path; startup already catches `LunaConfigError` -> exit 2 with the hint |

## 8. Out of scope (revisit later)

Repo map, `execute` sandbox, hooks, custom slash commands, web search,
per-turn cost in currency, async subagents, approval prompts for subagent
tool calls, `luna sessions rm` / `sessions.db` pruning.
