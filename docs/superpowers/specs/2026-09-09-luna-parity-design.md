# Luna — harness parity (LSP, formatters, custom commands, git undo, …) — Design

**Date:** 2026-09-09
**Status:** Approved (design), pending implementation plan — implementation deferred
**Version target:** 0.3.0 (new user-facing capabilities)
**Follows:** `2026-09-08-luna-hardening-design.md` (0.2.1, on `main`)

## 1. Goal

Adopt eight capabilities the comparison with **opencode** (and the wider field —
Claude Code, Aider, Cline/Roo, Codex CLI) surfaced as Luna's remaining gaps.
Each is a thin, isolated module. `deepagents` / `langgraph` imports stay confined
to `luna/agent.py`, `luna/session.py`, `luna/persistence.py`, `luna/toolguard.py`.

The eight:

1. **LSP** — (a) diagnostics fed into context after edits; (b) `goto_definition` /
   `find_references` / `hover` as agent tools.
2. **Formatters** — auto-run `ruff format` / `prettier` / `gofmt` / … after a
   mutating turn so diffs stay clean.
3. **Custom slash commands** — `.luna/commands/<name>.md` prompt templates with
   `$ARGUMENTS`, `` !`shell` `` injection, and `@file` expansion.
4. **Undo rework** — a git tree snapshot per turn (shadow ref); `/undo` reverts
   files **and** truncates the conversation to that point; `/redo`. The existing
   JSON file-journal becomes the non-git fallback.
5. **Model registry** — a vendored `luna/models.toml` (context window + price per
   model); `$` cost in the indicator and `/usage`.
6. **Plan mode** — `/plan` blocks mutating tool calls (no rebuild).
7. **`@agent`** — `@<subagent> <text>` in the REPL delegates to that subagent.
8. **`luna "prompt" --json`** — structured one-line JSON output for CI/scripting.

Out of scope: client/server split + SDK, `execute` OS sandbox, hooks, web search,
image input, `/share`, TUI themes/keybindings.

## 2. Verified framework facts

- `multilspy` (`from multilspy import SyncLanguageServer`) — a sync LSP client
  that auto-downloads the language server binary for the configured language,
  managed by a `with server.start_server(): …` context manager. Exposes
  `request_definition(file, line, col)`, `request_references(...)`,
  `request_hover(...)`. Supported: python, typescript, javascript, go, rust,
  java, csharp, ruby, php, cpp, dart, kotlin, elixir. It does **not** surface
  `publishDiagnostics` cleanly — hence diagnostics come from a separate check
  command, not from `multilspy`.
- `agent.update_state(config, {"messages": [RemoveMessage(id=...), ...]})` works
  on the deepagents `AgentState` (standard `add_messages` reducer) — proven by
  `/compact` in 0.2.1. `RemoveMessage(id=REMOVE_ALL_MESSAGES)` clears all;
  removing by explicit id removes those; appending `HumanMessage`/`AIMessage`
  with fresh ids re-adds them (this is how `/redo` re-adds removed messages).
- `create_deep_agent(..., middleware=[...])` — a `@wrap_tool_call` middleware can
  block a tool call by returning a `ToolMessage` instead of calling `handler`
  (proven by `tool_guard`'s deny path). The middleware closes over mutable state
  it reads each call — this is how plan mode toggles without a rebuild.
- `create_deep_agent(..., tools=[...])` — extra `@tool` functions are added to
  the main agent's toolset (proven by `EXTENSION_TOOLS`). Read-only tools need no
  `interrupt_on` entry.
- git plumbing for a worktree-safe snapshot: `GIT_INDEX_FILE=<tmp> git add -A`
  then `git write-tree` then `git commit-tree <tree> -p <HEAD>` — writes a commit
  object without touching the user's index or working tree; update a private ref
  `refs/luna/undo/<session_id>` to point at it. Restore with
  `git restore --source=<sha> --worktree --staged=false -- .` (or
  `git checkout <sha> -- .` on older git).

Re-verify during implementation: exact `multilspy` version + its result dict
shape; that `git restore --source` is available (git ≥ 2.23) with a
`git checkout <sha> -- .` fallback; that a `commit-tree` on a repo with no
commits yet (empty HEAD) degrades gracefully.

## 3. Feature designs

### 3.1 LSP diagnostics (`luna/diagnose.py`)

- `diagnose.detect(workdir: str) -> str` — returns a check command or `""`:
  `ruff` on PATH + a `pyproject.toml`/`ruff.toml` → `ruff check --output-format json`;
  else `pyright` on PATH + Python markers → `pyright --outputjson`;
  `tsc` + `tsconfig.json` → `tsc --noEmit --pretty false`;
  `go.mod` → `go vet ./...`;
  `Cargo.toml` → `cargo check --message-format short`.
- `diagnose.run(command: str, workdir: str, paths: list[str]) -> str` — run the
  command (append `paths` when the tool accepts a path list: ruff, pyright, tsc;
  otherwise run project-wide), parse stdout into a compact
  `path:line:col: severity: message` list, keep only entries for `paths` when
  project-wide, cap at ~40 lines. `subprocess.run(shell=True, timeout=120)`.
  Never raises — a parse failure returns the raw tail.
- Config: `LunaConfig.diagnose_command: str = "auto"` — `""` disables, `"auto"`
  → `detect(workdir)`, anything else is a literal command. `agent.diagnose_command`
  settable.
- Wiring in `session.py`, after a mutating turn (`tool_names & _MUTATING`), in
  this order: **format → diagnose → verify**. `diagnose` output, when non-empty,
  is (a) printed dim, and (b) stashed on a `pending_diagnostics: list[str]` that
  `run_repl` prepends to the *next* turn's payload as
  `<diagnostics>\n…\n</diagnostics>` (same mechanism as pinned files), then
  cleared. This is visibility, not an auto-fix loop.
- `/diagnose` REPL command — run on demand against the session's touched files
  (or the whole project when none), print the result.
- The set of "touched files this turn" comes from parsing `write_file`/`edit_file`/
  `delete` tool-call args in `_stream_turn` — add a `touched: set[str]` to its
  return tuple (5-tuple), or track it on the `TurnUsage`-adjacent bookkeeping.

### 3.2 LSP navigation tools (`luna/lspnav.py`)

- New optional dependency: `pyproject.toml` extra `lsp = ["multilspy>=0.0.10"]`
  (pin the exact version at implementation time).
- `lspnav.detect_language(workdir: str) -> str | None` — `pyproject.toml` →
  `"python"`, `tsconfig.json`/`package.json` → `"typescript"`, `go.mod` → `"go"`,
  `Cargo.toml` → `"rust"`; config override `LunaConfig.language: str = ""`.
- `lspnav.make_tools(workdir: str, language: str) -> list`:
  - `goto_definition(file: str, line: int, symbol: str) -> str`
  - `find_references(file: str, line: int, symbol: str) -> str`
  - `hover(file: str, line: int, symbol: str) -> str`
  Each: resolve `file` against `workdir`, read that line, find the first column
  where `symbol` occurs, call the corresponding `SyncLanguageServer` request,
  format results as a compact `relpath:line` list (or the hover text). Errors →
  a one-line "LSP unavailable: …" string, never raise.
- Server lifecycle: a module-level `_SERVERS: dict[tuple[str, str], object]`
  cache. First tool call for a `(workdir, language)` lazily does
  `srv = SyncLanguageServer.create(...)`, enters `srv.start_server()` via a
  persistent context (kept open), registers an `atexit` to close it. A failed
  start caches `None` and every tool returns "LSP unavailable".
- `agent.py` `build_agent`: `if lspnav is importable and detect_language(...):`
  append `lspnav.make_tools(...)` to `tools=[...]`; else `on_warn` once (only when
  `multilspy` is missing AND a language was detected). These tools are NOT in
  `INTERRUPT_TOOLS`.
- Prompt: one line in `LUNA_SYSTEM_PROMPT` — "when a symbol's origin or callers
  matter, use `goto_definition` / `find_references` rather than grepping."

### 3.3 Formatters (`luna/fmt.py`)

- `fmt.detect(workdir: str) -> str` — `ruff` + Python markers → `ruff format`;
  `black` → `black -q`; `prettier` + `package.json` → `prettier -w`;
  `gofmt` + `go.mod` → `gofmt -w`; `rustfmt` + `Cargo.toml` → `rustfmt`.
- `fmt.run(command: str, workdir: str, paths: list[str]) -> list[str]` — run
  `<command> <paths>` (`shell=True`, timeout 60), return the list of paths it
  reported changing (parse tool output where possible; otherwise return `paths`
  and let the caller diff). Never raises.
- Config: `LunaConfig.format_command: str = "auto"` (`""` disables). Settable
  `agent.format_command`.
- Wiring: `session.py`, first step of the post-mutation hook (before diagnose).
  Print `[dim]⌁ formatted {n} file(s)[/]` when `n > 0`.

### 3.4 Custom slash commands (`luna/usercmd.py`)

- `usercmd.command_dirs(workdir, env) -> [Path]` — `config_dir()/commands`,
  `<workdir>/.luna/commands`.
- `usercmd.UserCommand` — `name`, `description`, `model` (`str | None`), `body`.
- `usercmd.load(workdir, env=None) -> dict[str, UserCommand]` — read every
  `*.md`; optional `---`-fenced YAML-ish frontmatter (reuse `skills._frontmatter`
  style: `key: value` lines) for `description` / `model`; the rest is `body`.
  Project overrides user on name clash. A malformed file is skipped with no raise.
- `usercmd.expand(cmd: UserCommand, arg: str, workdir: str) -> str`:
  - `$ARGUMENTS` → `arg`
  - `` !`shell command` `` → `subprocess.run(..., shell=True, timeout=30)` stdout
    (trimmed, capped ~4k), each occurrence
  - then `context.expand_mentions(text, workdir)` for `@file`
- `DispatchResult` gains `prompt: str | None = None`.
- `commands.dispatch`: on a `_TABLE` miss AND `name` (sans `/`) in the loaded
  user-command map → `return DispatchResult(prompt=usercmd.expand(cmd, arg, workdir))`
  (and carry a `model` override if set — see below). Unknown `/foo` still prints
  "unknown command".
- `run_repl`: when `res.prompt is not None`, treat it as the turn's `line`
  (skip `expand_mentions` re-run since `expand` already did it; but pinned files
  still prepend). A per-command `model` override: temporarily swap `config.model`,
  rebuild, run, restore + rebuild — OR simpler, defer model override to a
  follow-up and just note it. **Decision: ship without the per-command model
  override in 0.3.0** (frontmatter is parsed and stored but ignored); revisit.
- The command map is loaded once in `run_repl` and passed on `CommandContext`
  (new field `user_commands: dict | None = None`); `/reload` reloads it.
- `/commands` lists them; `_print_help` shows a "custom:" section.

### 3.5 Undo rework — git snapshot + conversation truncation (`luna/undo.py`)

**Journal formats coexist, chosen per workdir:**

**Git path** — `gitinfo.is_git_repo(workdir)` is true:

- `undo.begin_turn(workdir, session_id, message_count: int) -> None` — called
  from `session.py` *before* each non-slash turn (and before a `/compact`,
  `/init`, or verify fix-up turn is not snapshotted — only user turns):
  - `tree = _snapshot_tree(workdir)` — `GIT_INDEX_FILE=<tmp>` + `git add -A` +
    `git write-tree`
  - `parent = git rev-parse HEAD` (or none if unborn)
  - `sha = git commit-tree <tree> [-p <parent>] -m "luna turn <n>"`
  - `git update-ref refs/luna/undo/<session_id> <sha>`
  - append `{turn, pre_sha: <the sha>, message_count}` to
    `.luna/undo/<session_id>/turns.json` (the "undo stack")
  - clear the redo stack file
- `undo.undo(workdir, session_id, agent, config) -> str | None`:
  - pop the newest turn record from the undo stack
  - capture the *current* tree as `post_sha` (another `_snapshot_tree` +
    `commit-tree`) and the messages added since `message_count`
  - `git checkout <record.pre_sha> -- .` (restore worktree files;
    `--` scoped, does not move HEAD)
  - `agent.update_state(config, {"messages": [RemoveMessage(id=m.id) for m in
    added]})` — truncate the conversation
  - push `{messages: [serialised], post_sha, message_count}` to the redo stack
  - return `f"undid turn {n} — {len(added)} messages, files restored"`
- `undo.redo(workdir, session_id, agent, config) -> str | None`:
  - pop the newest redo record
  - `git checkout <record.post_sha> -- .`
  - re-append the stashed messages via `agent.update_state` (new ids — build
    `HumanMessage`/`AIMessage`/`ToolMessage` from the serialised form; a
    tool-call/tool-result pair must stay together and valid, so store enough to
    reconstruct: `type`, `content`, `tool_calls`, `tool_call_id`, `name`)
  - move the record back onto the undo stack
  - return a note
- `undo.session_diff(workdir, session_id) -> str` (git path) —
  `git diff <first pre_sha in the stack>..<working tree> -- .` rendered.
- Any new user turn after an `/undo` clears the redo stack (in `begin_turn`).
- Cleanup: `refs/luna/undo/<id>` and `.luna/undo/<id>/` are pruned by the
  existing `undo.gc` (extended to also `git update-ref -d` stale refs).

**Fallback path** — not a git repo:

- The current per-file JSON journal stays: `snapshot()` in `toolguard.py`,
  `undo_last()` reverts one file, `/redo` prints "redo needs a git repo",
  `/diff` reconstructs from the journal as today.

**Wiring:**
- `toolguard.py`: `snapshot(...)` is called only when the workdir is NOT a git
  repo (pass a `git: bool` flag into `tool_guard`, or check `is_git_repo` inside
  — cache the result). On the git path the middleware does no snapshotting.
- `session.py`: `undo.begin_turn(workdir, session_id, len(current_messages))`
  right before building the non-slash `payload`. `current_messages` from
  `agent.get_state(turn_config).values.get("messages", [])`.
- `commands.py`: `_undo` → `undo.undo(ctx.workdir, ctx.session_id, ctx.agent,
  {"configurable": {"thread_id": ctx.thread_id}})`; new `_redo`; `_diff` →
  `undo.session_diff(...)`. `/undo` still confirms first via `ctx.input_fn`.
- `HELP` gains `/redo`.

### 3.6 Model registry + cost (`luna/models.toml`, `luna/usage.py`)

- `luna/models.toml` (packaged data file, `[tool.hatch.build] include`):
  ```toml
  ["claude-sonnet-4"]
  window = 200000
  input  = 3.0      # USD per 1M input tokens
  output = 15.0
  ["gpt-5"]
  window = 400000
  input  = 1.25
  output = 10.0
  # … ~20 entries, keyed by an id substring like the current _WINDOWS map
  ```
- `usage.py`:
  - `_registry() -> dict` — `tomllib.load` the packaged file once, cached; on any
    error return `{}`.
  - `context_window(provider, model)` — look up the registry first (substring
    match), then the current hard-coded `_WINDOWS` as a secondary, then `_FALLBACK`.
  - `price(provider, model) -> tuple[float, float] | None` — `(input, output)`
    USD/1M from the registry, or `None`.
  - `SessionUsage.cost(provider, model) -> float | None` — `sum over turns of
    (in_tokens/1e6 * in_price + out_tokens/1e6 * out_price)`; `None` when no price.
  - `indicator_line(...)` appends ` · $0.0123` when a cost is known.
  - `/usage` prints the per-turn table with a cost column and a session total.
- Config: `LunaConfig.pricing: dict = {}` from `[model.pricing]` — e.g.
  `[model.pricing] "my-model" = { input = 2, output = 6, window = 128000 }` —
  merged over the registry. Not `_SETTABLE` (nested table).

### 3.7 Plan mode (`luna/toolguard.py`, `luna/commands.py`, `luna/agent.py`)

- `tool_guard(rules, workdir, session_id="", plan=None)` — `plan` is a
  zero-arg callable returning `bool` (or `None` = never plan mode). In `_guard`,
  after the deny check, `if plan is not None and plan() and name in
  {"write_file","edit_file","delete","execute"}: return ToolMessage("plan mode
  is on — refusing to {name}. Run /plan off to make changes.", tool_call_id=…)`.
- `agent.build_agent(..., plan_flag: Callable[[], bool] | None = None)` → passes
  it to `tool_guard`.
- `run_repl`: owns `plan_state = [False]`; `plan_flag = lambda: plan_state[0]`.
  Passed into `_rebuild` (so `/reload` keeps it) — meaning `cli.main`'s
  `_rebuild` closure and `run_repl` share the list. Add `plan_flag` param to
  `run_repl` and thread it from `cli.main`.
- `commands._plan(ctx, arg)` — `ctx` gets a `plan_state: list[bool] | None`
  field; `/plan` toggles `plan_state[0]`, `/plan on` / `/plan off` set it. Prints
  the new state.
- `run_repl` prompt: `"luna (plan) › "` when `plan_state[0]`, else `"luna › "`.
- `HELP` gains `/plan`.

### 3.8 `@agent` mention (`luna/session.py`)

- In `run_repl`, before building a non-slash payload: if `line` matches
  `^@([\w-]+)\s+(.+)` and group 1 is a known subagent name
  (`{n for n, _ in subagent_summaries(workdir)}`), rewrite `line` to:
  `Delegate this to the '<name>' subagent using the task tool: <rest>`.
  Otherwise leave `@name` alone (it may be an `@file` mention — those are
  handled by `expand_mentions` and don't have a following space+text pattern
  that collides meaningfully; a bare `@path` has no space).
- Edge: `@file.py explain this` — `file.py` is not a subagent name → not
  rewritten → `expand_mentions` turns `@file.py` into the file contents. Fine.
- Cache the subagent-name set per REPL (refresh on `/reload`).

### 3.9 `luna "prompt" --json` (`luna/cli.py`, `luna/session.py`)

- `cli.build_parser`: `--output-format {text,json}` (default `text`), `--json`
  sets it to `json`.
- `session.run_once(..., output: str = "text")`:
  - `output == "json"`: build the console as a quiet one (no turn framing —
    pass a flag that suppresses `open_turn`/`close_turn`/`tool_line`/indicator),
    collect `tool_names`, `turn_usage`, final text; at the end return a dict and
    let `cli` print `json.dumps(...)` — OR `run_once` prints it itself when
    `output == "json"`. **Decision:** `run_once` returns its usual `str` in text
    mode; in json mode it prints one JSON line to stdout and returns "". Shape:
    ```json
    {"text": "...", "tools_used": ["read_file","write_file"],
     "usage": {"input": 1200, "output": 300, "total": 1500},
     "cost_usd": 0.0042, "thread_id": "abc123"}
    ```
  - The rich console must not interleave: in json mode, route all non-result
    output (warnings, tool lines) to stderr or suppress it.

## 4. Config additions

```toml
[agent]
diagnose_command = "auto"     # "" off, "auto" detect, or a literal command
format_command   = "auto"
language         = ""         # override LSP language detection

[model.pricing.my-model]
input  = 2.0
output = 6.0
window = 128000
```

New `_SETTABLE`: `agent.diagnose_command` (str), `agent.format_command` (str),
`agent.language` (str). `[model.pricing]` is hand-edited (nested).

New `LunaConfig` fields: `diagnose_command: str = "auto"`,
`format_command: str = "auto"`, `language: str = ""`, `pricing: dict = {}`.

## 5. CLI / REPL surface additions

CLI: `--output-format {text,json}` / `--json`.

REPL: `/plan` (+ `/plan on|off`), `/diagnose`, `/redo`, `/commands`, and any
`.luna/commands/*.md` as `/<name>`.

## 6. Dependencies

`pyproject.toml`: new optional-dependency group `lsp = ["multilspy>=…"]`, added
to the `all` extra. `models.toml` packaged via hatch `include`.

## 7. Implementation order

1. `models.toml` + `usage.py` registry/cost + `/usage` cost column.
2. `fmt.py` + config + post-mutation hook (format step).
3. `diagnose.py` + config + hook (diagnose step) + `<diagnostics>` injection + `/diagnose`.
4. `lspnav.py` + `lsp` extra + `agent.py` tool wiring + prompt line.
5. `usercmd.py` + `DispatchResult.prompt` + `dispatch` fallthrough + `run_repl`
   prompt-injection + `/commands`.
6. Plan mode: `tool_guard` `plan` param + `build_agent` + `run_repl`/`cli`
   `plan_state` + `/plan` + prompt.
7. `@agent` rewrite in `run_repl`.
8. `--output-format json` in `cli` + `run_once`.
9. Undo git infra: `undo.begin_turn` / `_snapshot_tree` / shadow ref / `turns.json`;
   `toolguard` snapshot only on non-git; `session.py` calls `begin_turn`.
10. Undo `/undo` (files + conversation truncation) + `/redo` + redo stack.
11. Undo `/diff` via `git diff`; non-git fallback verified; `gc` prunes refs.
12. Docs + `CHANGELOG` `## [0.3.0]` + README + AGENTS.md + version bump.

Each task: its own tests, `ruff` clean, full `pytest` green on the machine.

## 8. Testing strategy

- Reuse `FakeToolCallingModel`; no network. The `lsp` extra is NOT installed in
  CI by default — `lspnav` tests `pytest.importorskip("multilspy")` or test the
  detection/formatting logic with the server stubbed.
- `diagnose` / `fmt` tests point the command at a scripted `python -c` one-liner
  (`sys.executable`, per the 0.2.1 lesson) that emits known JSON / exits non-zero.
- Undo git tests: `subprocess.run(["git","init"], cwd=tmp)` + a couple of commits;
  assert `refs/luna/undo/<id>` is created, `/undo` restores file content AND the
  thread's message count drops, `/redo` restores both.
- `usercmd`: write `.md` files under a tmp `.luna/commands/`, assert `$ARGUMENTS`
  / `` !`echo x` `` / `@file` expansion.
- Plan mode: build an agent with `plan_flag=lambda: True`, a fake model that
  emits a `write_file` call → assert the blocking ToolMessage.
- `--json`: `cli.main(["--json","..."])` with a fake model → assert stdout is one
  parseable JSON object with the expected keys.

## 9. Risks

| Risk | Mitigation |
| --- | --- |
| `multilspy` downloads a large language-server binary on first use (slow, network) | opt-in `lsp` extra; `agent.py` skips the tools with an `on_warn` when it's absent; first-call latency documented |
| git `commit-tree` on an unborn HEAD (fresh `git init`, no commits) | `_snapshot_tree` handles the no-parent case; `/undo` to a pre-first-commit tree = restore to empty/initial |
| `git checkout <sha> -- .` leaves deleted-since files untracked, not removed | use `git restore --source=<sha> --worktree -- .` (git ≥ 2.23) which removes them; fall back to `git checkout` + a `git clean` guarded by the snapshot's file list |
| `/redo` message reconstruction produces an invalid history (tool call without result) | store whole turn segments (user msg → … → final ai) and re-append them as one `update_state`; a `/redo` that can't rebuild a valid segment refuses with a note |
| `diagnose`/`fmt` auto-detect runs the wrong tool in a polyglot repo | config override always wins; `"auto"` picks one deterministically by marker priority; `/diagnose` shows what ran |
| post-mutation hook (format+diagnose+verify) makes a turn slow | each step has a timeout; `format_command=""`/`diagnose_command=""` disable; they run once per turn, not per tool call |
| `DispatchResult.prompt` + pinned files + `@agent` + `<diagnostics>` all mutate the outgoing turn text | one well-ordered assembly point in `run_repl`: `[diagnostics?] + [pinned?] + (user-command prompt OR @agent-rewrite OR expand_mentions(line))` |

## 10. Out of scope (revisit later)

Client/server + `luna serve` + SDK; `execute` OS sandbox; hooks
(PreToolUse/PostToolUse); web search/fetch; image input; `/share`; per-command
`model` override; TUI themes / configurable keybindings; `@file` fuzzy
tab-completion.
