# Luna — competitive feature set — Design

**Date:** 2026-09-07
**Status:** Approved (design), pending implementation plan
**Scope:** 9 features that close the gap with Claude Code / Codex CLI / Aider / Cline
without abandoning Luna's "reads start to finish" identity.

## 1. Goal

Add the table-stakes capabilities other coding-agent harnesses ship that Luna
lacks, plus a few cheap differentiators. No orchestration/graph layer — every
feature is a thin, isolated module. deepagents / langgraph imports stay confined
to `luna/agent.py` and `luna/session.py` (plus one new middleware module wired
in `agent.py`).

The nine features (numbering used throughout):

1. Session persistence + `luna --continue` / `--resume` / `/resume`
2. Context/token indicator + `/compact`
3. `/diff` + git dirty-check + `/undo`
4. `@file` mentions + `/add`
5. Permission rules (allow/deny + "always")
6. `.luna/memory/` tiers + `remember` tool
7. Post-edit verification loop
8. `/init` — generate AGENTS.md
9. `/model` + `/provider` hot-swap (+ optional fast model for subagents)

Out of scope: repo map / codebase index (ROI-list #10), the cognitive/orchestrator
layer, model routing beyond the fast-model tier, web search, hooks, custom slash
commands, image input.

## 2. Verified framework facts (deepagents 0.7.13 / langchain 1.4 / langgraph 1.2)

- `langchain.agents.middleware` exposes the `@wrap_tool_call` decorator:
  `def mw(request, handler)` where `request.name` / `request.args` describe the
  pending call and `handler(request)` runs it. Returning a `ToolMessage` instead
  of calling `handler` blocks the call. This middleware runs for the main agent's
  tool calls (not calls made inside subagents).
- `AgentMiddleware` subclasses expose `before_agent` / `after_agent` /
  `before_model` / `after_model` hooks; state is updated by returning a dict,
  never by mutation.
- Persistent checkpointer: `from langgraph.checkpoint.sqlite import SqliteSaver`
  (package `langgraph-checkpoint-sqlite`).
  `SqliteSaver(sqlite3.connect(path, check_same_thread=False))` is a usable
  non-context-manager form; call `.setup()` once.
- deepagents `create_deep_agent` already installs a `SummarizationMiddleware`
  that auto-compacts around 85% of the context window. `/compact` (feature 2) is
  a manual trigger layered on top, not a reimplementation.
- `create_deep_agent(..., memory=[...])` takes a list of file paths (relative to
  the backend root) whose contents are injected into the system prompt.
- AIMessage / AIMessageChunk carry `usage_metadata`
  (`input_tokens`, `output_tokens`, `total_tokens`) when the provider reports it.
- Resume still requires the same `thread_id` in `config["configurable"]` and a
  checkpointer.

Re-verify during implementation: exact `wrap_tool_call` request attribute names
in the pinned versions; that SqliteSaver serialises deepagents state without
custom serde; whether `usage_metadata` arrives on chunks or only on the final
message for each provider.

## 3. Architecture

### 3.1 New modules

| Module | Purpose | Depends on |
| --- | --- | --- |
| `luna/persistence.py` | `SqliteSaver` factory + `luna_sessions` index table (plain sqlite) | stdlib `sqlite3`, langgraph |
| `luna/usage.py` | token accounting, `model -> context window` map, formatting | langchain messages |
| `luna/undo.py` | snapshot journal read/write, `/diff` rendering, `/undo` restore | stdlib `difflib`, `json` |
| `luna/gitinfo.py` | `is_git_repo`, `dirty_paths` (one `git status --porcelain` call) | stdlib `subprocess` |
| `luna/permissions.py` | load + merge `allow`/`deny` rules, `match(tool, args) -> allow\|deny\|None`, append-rule | stdlib `tomllib`, `fnmatch` |
| `luna/memory.py` | discover `.luna/memory/*.md`, `append_note(kind, topic, note)` | stdlib `pathlib` |
| `luna/initgen.py` | build the AGENTS.md-generation prompt + existing-file guard | — |
| `luna/context.py` | expand `@path` tokens, manage session-pinned files (`/add`) | stdlib `pathlib`, `shlex` |
| `luna/commands.py` | REPL slash-command dispatch table (refactor out of `session.py`) | the above + `session` helpers |

### 3.2 Modified modules

- `luna/agent.py` — build and attach the `luna_tool_guard` middleware
  (undo snapshots + permission deny); extend `memory=[...]`; pass `fast_model`
  into `load_subagents`; accept `checkpointer` + `session_id` from the caller.
- `luna/session.py` — accumulate usage; run the verification loop after mutating
  turns; consult permission `allow` rules before prompting; delegate slash
  commands to `luna/commands.py`; accept a starting `thread_id`.
- `luna/cli.py` — `-c/--continue`, `--resume [ID]` flags; `luna init` subcommand;
  wire the SqliteSaver + session index; resume/list flow.
- `luna/config.py` — new settable keys `model.fast`, `agent.verify_command`;
  new `[permissions]` section is read by `permissions.py`, not folded into
  `LunaConfig` (list-merge, not override).
- `luna/prompts.py` — mention memory files, the `remember` tool, and the
  verification expectation.
- `luna/ui/approve.py` — add the `[a] always` choice; return an
  `{"type": "approve", "always": <rule>}` shaped decision for `session.py` to
  persist.
- `luna/extension_tools.py` — add the `remember` tool (approval-gated).
- `pyproject.toml` — add `langgraph-checkpoint-sqlite` to base deps.

### 3.3 Data / files on disk

| Path | Owner | Content |
| --- | --- | --- |
| `~/.config/luna/sessions.db` | `persistence.py` | langgraph checkpoints + `luna_sessions` table |
| `<workdir>/.luna/undo/<session>/NNNN.json` | `undo.py` | `{tool, path, before, ts}` per mutating call |
| `<workdir>/.luna/permissions.toml` | `permissions.py` | project `allow`/`deny` lists (also read from `config.toml [permissions]`) |
| `<workdir>/.luna/memory/*.md` | `memory.py` / `remember` | `project.md`, `conventions.md`, `decisions.md`, `failures.md` |

`.luna/undo/` is added to the project `.gitignore` by `/init` and is added to
this repo's own `.gitignore` in the first implementation step.

## 4. Feature designs

### 4.1 Session persistence (#1)

- `persistence.checkpointer()` -> `SqliteSaver` over `~/.config/luna/sessions.db`
  (`check_same_thread=False`, `.setup()` once). `cli.py` creates it and passes it
  to `build_agent`; the REPL keeps it for the process lifetime.
- `persistence.SessionIndex` wraps the `luna_sessions(thread_id TEXT PRIMARY KEY,
  workdir TEXT, created REAL, updated REAL, title TEXT)` table:
  `record(thread_id, workdir, title)`, `touch(thread_id)`,
  `latest_for(workdir) -> row | None`, `list(workdir=None, limit=20)`.
- `title` = first user message, whitespace-collapsed, truncated to ~72 chars.
  Written on the first turn of a thread; `touch` updates `updated` each turn.
- CLI:
  - `-c` / `--continue` — resolve `latest_for(cwd)`; error cleanly if none.
  - `--resume` — no value: print the numbered `list(cwd)` and read a choice
    (respects `--no-input`: error instead). With a value: treat as thread_id or
    list index.
- REPL: `/sessions` prints `list(cwd)`; `/resume` = list + pick, swaps the active
  `thread_id`. On any resume, print a recap: role-prefixed last ~6 messages
  pulled from `agent.get_state(config)`.
- `/new` still generates a fresh thread_id and a fresh index row on first turn.
- Compatible with `/reload` and model swap: those call `rebuild()` with the same
  checkpointer and keep the current `thread_id`, so history persists.
- Tests (`test_persistence.py`): index CRUD; `latest_for` ordering; `--continue`
  picks newest for workdir; `--resume` index/id parsing; `--no-input` +
  `--resume` errors; thread survives `rebuild()`.

### 4.2 Context/token indicator + `/compact` (#2)

- `usage.TurnUsage` accumulates `usage_metadata` seen on model chunks/messages
  during one turn; `usage.SessionUsage` sums turns.
- `usage.context_window(provider, model) -> int` — small static map keyed by
  known model-id substrings; fallback `200_000`.
- After `close_turn`, `session.py` prints one dim line:
  `ctx ~{used}/{window} · turn {in} in / {out} out · session {total}`.
  `used` is the last known prompt-token count for the thread (from the latest
  model message's `input_tokens`).
- `/usage` — full breakdown (per-turn list + totals).
- `/compact`:
  1. Run one hidden turn on the current thread: "Summarise this whole session as
     a handoff note: goal, decisions made, files touched, current state, open
     questions. Be dense."
  2. Capture the assistant text.
  3. Allocate a new `thread_id`, update the same `luna_sessions` row to point at
     it, seed `messages=[{role:"user", content: "Continuing a compacted session.
     Handoff note:\n<summary>"}]`.
  4. Print a note that history was compacted.
- Built-in 85% auto-summarization is left untouched.
- Tests (`test_usage.py`): metadata accumulation across chunks; window lookup +
  fallback; `/compact` yields a new thread whose first message contains the
  summary; session row re-pointed.

### 4.3 `/diff` + dirty-check + `/undo` (#3)

- `luna_tool_guard` middleware (`agent.py`, `@wrap_tool_call`), for
  `request.name in {"write_file", "edit_file", "delete"}`:
  - resolve the target path against `workdir`;
  - read current bytes (or `None` if absent);
  - append `undo.journal_dir(session) / f"{n:04d}.json"` with
    `{"tool", "path", "before", "ts"}` **before** calling `handler`.
  - runs under `--yolo` too (middleware is independent of `interrupt_on`).
- `undo.session_diff(session, workdir)` — for each distinct path in the journal,
  unified diff between the earliest `before` and the current on-disk content.
- `undo.undo_last(session, workdir)` — pop the newest journal entry; if
  `before is None` delete the (created) file, else write `before` back. Returns a
  description. `/undo` confirms before acting and can be repeated.
- `gitinfo.dirty_paths(workdir)` — `[]` when not a git repo. `cli.py` prints a
  single non-blocking warning at startup when non-empty.
- `/diff` and `/undo` are REPL-only; `session` passes its `session_id` +
  `workdir` to the command dispatcher.
- Tests (`test_undo.py`): journal entry written when the guard wraps a fake
  mutating call; `undo_last` restores modified + deletes created; `session_diff`
  renders; `gitinfo.dirty_paths` detects a dirty tree and is empty off-git.

### 4.4 `@file` mentions + `/add` (#4)

- `context.expand_mentions(text, workdir) -> (text, notes)`:
  - tokenise with `shlex` so `@"a b.py"` works; match tokens starting with `@`.
  - existing file -> append
    `\n\n<attached: {relpath}>\n{content}\n</attached>` (content capped at
    100 KB with a `… (truncated)` marker);
  - directory -> append a shallow listing;
  - missing -> append `\n(@{path}: not found)`.
- `context.PinnedFiles` — set of session-pinned relpaths. `/add <path>...`,
  `/drop <path>...`, `/context` (list). Before each turn `session.py` prepends
  fresh contents of every pinned file (same `<attached:>` framing).
- Pins are session-scoped only (not persisted).
- Tests (`test_context.py`): mention expansion, quoted paths, missing file note,
  directory listing, size cap; `/add` then two turns both carry the file;
  `/drop` removes it.

### 4.5 Permission rules (#5)

- `permissions.load_rules(workdir)` merges `allow` + `deny` string lists from
  `config.toml [permissions]` and `<workdir>/.luna/permissions.toml`
  (concatenate, dedupe, order: user config then project).
- Rule syntax `"<tool>:<pattern>"`. `permissions.match(tool, args) ->
  "allow" | "deny" | None`:
  - `execute` -> `fnmatch(command_string, pattern)`;
  - file tools -> `fnmatch(relpath, pattern)`;
  - `deny` wins over `allow` when both match.
- Enforcement split:
  - **deny** in `luna_tool_guard` middleware: return
    `ToolMessage("blocked by permission rule: <rule>", tool_call_id=...)`
    without calling `handler`. Effective even under `--yolo`.
  - **allow** in `session.collect_decisions`: if `match(...) == "allow"`,
    emit `{"type": "approve"}` without prompting and print
    `⚙ {tool} · auto (rule)`.
- `ui/approve.py` `[a] always`:
  - file tools -> rule `"{tool}:{relpath}"`;
  - `execute` -> propose `"execute:{first_word} *"`, let the user edit the
    pattern inline, allow empty to fall back to the exact command.
  - decision dict gains `{"always": "<rule>"}`; `session.py` calls
    `permissions.append_project_rule(workdir, rule)` (writes
    `.luna/permissions.toml`) and reloads the in-memory rule set.
- Tests (`test_permissions.py`): allow auto-approves; deny blocks and also blocks
  under `--yolo`; deny-beats-allow; `execute` vs path matching; `append_project_rule`
  round-trips and is picked up.

### 4.6 `.luna/memory/` tiers + `remember` tool (#6)

- `memory.memory_files(workdir)` -> sorted existing `.luna/memory/*.md`
  (relpaths). `agent.build_agent` sets
  `memory = ["AGENTS.md"] * exists + memory_files(workdir)`.
- `memory.append_note(workdir, kind, topic, note)` -> appends
  `\n## {date} — {topic}\n\n{note}\n` to `.luna/memory/{kind}.md`
  (`kind in {"project","conventions","decisions","failures"}`), creating the file
  and `.luna/memory/` as needed.
- `remember` tool (`extension_tools.py`, approval-gated via
  `EXTENSION_INTERRUPTS`):
  `remember(kind: Literal[...], topic: str, note: str) -> str`. Returns
  `"noted in .luna/memory/<kind>.md. Run /reload to load it into context."`.
- `prompts.py`: add a line — when Luna hits a dead end or makes a load-bearing
  decision, record it with `remember` (failures especially).
- Tests (`test_memory.py`): discovery order; `append_note` targets the right file
  with a dated header; `remember` is approval-gated; new files land in `memory=`.

### 4.7 Post-edit verification loop (#7)

- Config `agent.verify_command` (string, default `""` -> feature dormant).
- `session.py`, after a turn where at least one of `write_file`/`edit_file`/
  `delete`/`execute` (mutating) ran:
  1. run `verify_command` in `workdir` (`subprocess.run`, capture, timeout ~300s);
  2. exit 0 -> dim `✓ verify ok`, done;
  3. non-zero -> print the tail (~40 lines), then feed a follow-up user turn:
     `"`verify_command` failed (exit N). Output:\n<tail>\nFix it."`, stream that
     turn (approvals as normal), then re-run the command **once**;
  4. still failing -> hand control back with
     `⚠ verify still failing after 1 retry`.
- `/verify` runs the command on demand at any point (no auto-fix).
- One retry cap is hard-coded (matches the approved decision).
- Tests (`test_verify.py`): passing command -> no follow-up; failing then passing
  -> exactly one retry turn; failing twice -> stops with the warning; empty
  command -> never runs; `/verify` manual path.

### 4.8 `/init` — generate AGENTS.md (#8)

- `initgen.init_prompt()` -> the fixed instruction: explore the repo, write
  `AGENTS.md` covering purpose, build/test/lint commands, structure, conventions;
  keep it concise; use read tools then `write_file`.
- `initgen.existing_action(workdir)` -> `"create" | "update"` depending on
  whether `AGENTS.md` exists; when it exists the prompt asks to revise/extend in
  place, not overwrite blindly.
- `luna init` subcommand: one-shot agent run with that prompt (normal approval
  flow). `/init` in the REPL: same, on the current thread.
- Tests (`test_initgen.py`): `luna init` dispatches a one-shot run whose prompt is
  `init_prompt()`; `existing_action` switches on the file; wiring via a fake model
  that emits a `write_file` call.

### 4.9 `/model` + `/provider` hot-swap + fast model (#9)

- REPL `/model <name>` and `/provider <key>`:
  - mutate the in-memory `LunaConfig` (`model` / `provider`);
  - `/provider` with no stored key and no env var -> print
    `luna config set-key <provider>` hint, do not swap;
  - otherwise call the existing `rebuild()`; keep `thread_id`; confirm.
- `/model` / `/provider` with no argument keep the current "show current" output.
- Fast model: `LunaConfig.fast_model` (from `config.toml [model] fast`, settable
  key `model.fast`). `agent.build_agent` passes it to
  `subagents.load_subagents(..., fast_model=...)`; built-in `researcher` /
  `reviewer` use it when set and when they have no explicit `model`. User-defined
  subagents are unchanged.
- Tests: `/model` swap keeps the thread and updates config; `/provider` without a
  key errors without swapping; `load_subagents` applies `fast_model` to built-ins
  only; `model.fast` is settable.

## 5. REPL command surface (after refactor)

`luna/commands.py` holds a dispatch table `{name: handler}`. Handlers receive a
small `CommandContext` (console, config, agent, session_id, thread_id ref,
pinned files, usage, rebuild callable) and may return a new agent / thread_id.

Existing: `/help /tools /agents /model /provider /reload /new /clear /exit`
New: `/sessions /resume /usage /compact /diff /undo /add /drop /context /verify /init`

`/help` output is regenerated from the table.

## 6. Config additions

```toml
[model]
fast = "claude-haiku-4-5"          # optional; subagents researcher/reviewer

[agent]
verify_command = "uv run pytest -q"  # optional; empty disables the loop

[permissions]
allow = ["execute:uv run pytest*", "execute:git status*"]
deny  = ["execute:git push*", "execute:rm -rf *", "write_file:.env"]
```

New settable keys for `luna config set`: `model.fast` (str),
`agent.verify_command` (str). `[permissions]` is edited by hand or grown via
`[a] always`; it is not exposed through `luna config set` (list semantics).

## 7. CLI additions

```
luna -c / --continue        resume the most recent session for the cwd
luna --resume [ID]          pick a past session (no ID: interactive list)
luna init                   generate / update AGENTS.md for this repo
```

Exit codes unchanged (0 / 1 / 2 / 130).

## 8. Implementation order

1. `.gitignore` += `.luna/`; add `langgraph-checkpoint-sqlite` dep.
2. `persistence.py` + wire SqliteSaver & index into `cli.py` (#1).
3. Refactor slash commands into `commands.py` (no behaviour change).
4. `usage.py` + indicator + `/usage` + `/compact` (#2).
5. `context.py` — `@file` + `/add` / `/drop` / `/context` (#4).
6. `permissions.py` + `luna_tool_guard` middleware (deny) + allow auto-approve +
   `[a] always` (#5).
7. `undo.py` + `gitinfo.py` — extend the middleware with snapshots; `/diff`,
   `/undo`, dirty-check (#3).
8. `memory.py` + `remember` tool + prompt line (#6).
9. `verify` loop + `/verify` (#7).
10. `/model` + `/provider` swap + fast model (#9).
11. `initgen.py` + `luna init` + `/init` (#8).

Each step: its own tests, `ruff check` + `ruff format --check` clean, full
`pytest` green. Framework imports stay in `agent.py` / `session.py` /
`persistence.py` / the middleware module.

## 9. Testing strategy

- Reuse the `FakeToolCallingModel` fixture; no network.
- New fixtures: a temp workdir with an initialised git repo (for `gitinfo` /
  dirty-check) and a temp `XDG_CONFIG_HOME` (already provided by `conftest.py`).
- The verification loop is tested with `verify_command` pointed at a scripted
  shell one-liner (`python -c "import sys; sys.exit(...)"`).
- `SqliteSaver` tests use a temp DB file and assert state survives a fresh
  `build_agent` call.
- CI matrix unchanged (3.11 / 3.12).

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| `SqliteSaver` fails to serialise deepagents state | Verify in step 2 on a real run; fall back to a custom serde or a trimmed state channel |
| `wrap_tool_call` request attribute names differ in pinned versions | Confirm against installed source in step 6; adapter shim if needed |
| `wrap_tool_call` does not fire for subagent tool calls | Documented limitation: undo / permissions cover the main agent only (subagents are read-only here) |
| `/compact` reseed loses exact tool history | Accepted trade-off (same as Claude Code); the summary keeps decisions + file list |
| `usage_metadata` missing for some providers (ollama, deepseek) | Indicator degrades to "n/a"; never raises |
| Verification loop + approvals could surprise the user mid-turn | Loop only runs when `verify_command` is set; the retry turn goes through the normal approval flow; hard 1-retry cap |
| Module count grows ~10 | Each is small, single-purpose, independently testable; `session.py` shrinks via the `commands.py` refactor |

## 11. Out of scope (revisit later)

Repo map / codebase index, cognitive/orchestrator layer, per-task model routing,
critic/risk-scoring gate, web search/fetch tool, hooks, custom slash commands,
image input, parallel async subagents.
