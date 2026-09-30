# Luna: every slash command in the TUI, with REPL turn parity

Date: 2026-09-30
Status: approved in conversation, awaiting written-spec review

## Goal

Every REPL slash command works in the TUI, and a TUI turn behaves exactly
like a REPL turn (undo journal, pinned files, `@file` / `@agent`,
format + diagnose + verify with one fix-up turn, auto-reload). Per-session
settings (model, provider, plan mode, pinned files, usage) are isolated
per session and persisted, so the future desktop client gets the same
behaviour through the same API.

## Decisions (from the conversation)

- `/model`, `/provider`, `/plan` affect **only the current session**.
- **Full turn parity** with the REPL on the server path.
- Architecture follows the analogues researched on 2026-09-30:
  one agent per session (Hermes: "fresh AIAgent per session"; session
  settings in SQLite), a single command registry shared by every surface
  (Hermes `COMMAND_REGISTRY`), one server entry point for commands
  (opencode `POST /session/:id/command`), UI-only commands handled by the
  client (OpenClaw TUI), read-only commands allowed mid-turn (Claude Code).
- One spec, **one implementation plan**.

## Current state (verified)

- The server builds one agent **per workdir** (`make_agent_factory`),
  without `session_id` or `plan_flag`: the undo journal and plan mode do
  not work through the TUI at all, and `tool_guard`'s `AnchorTracker`
  would be shared across sessions.
- The REPL turn pipeline lives inline in `run_repl`
  (`luna/core/session.py` ~514–580): `@agent` delegation,
  `expand_mentions`, `render_pinned`, pending diagnostics,
  `undo.begin_turn`, `dirty_paths` snapshot, usage, index record/touch,
  `_format_and_diagnose`, `_run_verification` (one fix-up turn),
  auto-reload. The server turn (`luna/server/turns.py`) has none of it.
- REPL commands (`luna/repl/commands.py`, 24 entries in `HELP`) print to a
  Rich console and read input via `arrow_pick` / `arrow_confirm`.
- The TUI handles only `/exit`, `/quit`, `/clear`, `/help`, `/commands`
  locally; everything else prints "not yet in the TUI".
- The TUI turn runs inside `on_input_submitted`, which blocks further
  input until the stream ends.

## 1. Session state and one agent per session

### Storage

New table in `sessions.db`:

```sql
CREATE TABLE IF NOT EXISTS luna_session_state (
    thread_id           TEXT PRIMARY KEY,
    workdir             TEXT NOT NULL,
    provider            TEXT,                        -- NULL = project config
    model               TEXT,                        -- NULL = project config
    plan                INTEGER NOT NULL DEFAULT 0,
    pinned              TEXT NOT NULL DEFAULT '[]',  -- JSON list of paths
    pending_diagnostics TEXT NOT NULL DEFAULT '',
    usage               TEXT NOT NULL DEFAULT '[]'   -- JSON list of per-turn {input, output, total}
)
```

Separate from `luna_sessions` because a `luna_sessions` row only exists
after the first message, while `/model` or `/plan` may run before it.
Accessed through `luna/core/session_state.py: SessionStateStore`
(best-effort like `SessionIndex`: an unavailable DB degrades to in-memory
defaults). `SessionState` is a dataclass with the columns above
(`pinned: list[str]`, `usage: SessionUsage` via existing
`luna.config.usage`).

### SessionRuntime

`luna/server/runtime.py` replaces the per-workdir agent registry:

- `RuntimeRegistry.get(thread_id, workdir) -> SessionRuntime`, LRU of 8;
  an evicted runtime is rebuilt from `SessionStateStore` on next access.
- `SessionRuntime` holds `state: SessionState`, a lazily built agent, a
  `phase` (`idle` / `turn` / `fixup`), the pending `PreparedTurn` while a
  turn is in flight, and an `asyncio.Lock`.
- The agent is built with `session_id=thread_id` (the REPL already uses
  the start thread id as its undo `session_id`, so journals are
  compatible), `plan_flag=lambda: runtime.state.plan`, the session's
  provider/model applied over `load_config({"workdir": wd}, cwd=wd)`, and
  the SQLite checkpointer.
- `runtime.rebuild()` rebuilds only this session's agent; on failure the
  previous provider/model are restored and the error is reported.
- One turn at a time per session: a second `/messages` while
  `phase != idle` → `409 {"error": "session_busy"}`.
- A workdir mismatch (runtime cached for another workdir) →
  `400 {"error": "workdir_mismatch"}`.

## 2. Shared turn engine

New module `luna/turn/engine.py`, used by both the REPL loop and the
server. Streaming stays transport-specific (REPL: console + inline
approvals; server: `iter_turn` + SSE + `/approve`).

- `prepare_turn(state, line, workdir, subagent_names) -> PreparedTurn`
  - `@agent text` → delegation prompt (existing `_AT_AGENT_RE` logic);
  - `expand_mentions`, `render_pinned(state.pinned)`, pending diagnostics
    prepended then cleared from `state`;
  - `undo.begin_turn(workdir, session_id, len(current_messages))`;
  - `dirty_before = gitinfo.dirty_paths(workdir)` in a git repo.
  - Returns `PreparedTurn(content, title_line, dirty_before)`.
- `finish_turn(state, prepared, outcome, cfg) -> FinishResult`
  (`outcome` = `TurnOutcome(usage, tool_names, reload_requested)`):
  - add usage to `state.usage`; `index.record` + `index.touch`;
  - if `tool_names & _MUTATING`: format + diagnose newly dirty paths
    (existing `_format_and_diagnose` logic, returning notices instead of
    printing) → `state.pending_diagnostics`; then `run_verify`;
  - `FinishResult(notices, fixup_prompt | None, reload: bool)`;
    `fixup_prompt` is the existing "The verify command … failed … Fix it."
    text.
  - `finish_fixup(cfg) -> list[Notice]` re-runs verify once:
    "✓ verify ok" or "⚠ verify still failing after 1 retry".
- `Notice = (level, text)`, level ∈ `info | ok | warn | error`.
- The REPL keeps its loop but calls `prepare_turn` / `finish_turn` /
  `finish_fixup` and prints notices; its visible output stays the same.
- `SessionState` is used by the REPL too (in-memory, not persisted), so
  both paths share one type.

### Server turn flow

- `POST /sessions/{id}/messages {workdir, content}`:
  `prepare_turn` → stream `iter_turn` → on `approval_needed` the stream
  ends with `phase=turn` kept; on completion → `finish_turn`.
- `POST /sessions/{id}/approve`: resumes; on completion runs the step
  that was pending for the current phase.
- A required fix-up turn streams **in the same SSE response**
  (`phase=fixup`); its approvals go through the same `/approve`; after it
  completes, `finish_fixup` runs.
- New SSE event `{"event": "notice", "level": ..., "text": ...}` for
  every `Notice`. `turn_done` is sent once, after the whole pipeline.
- Auto-reload rebuilds the session's agent after the turn and emits a
  notice.

## 3. Command registry and `/command`

New package `luna/commands/` replaces `luna/repl/commands.py`
(`luna/repl/commands.py` keeps `dispatch` as the REPL adapter).

```python
@dataclass
class Command:
    name: str
    help: str
    kind: Literal["ui", "read", "mutate", "prompt"]
    run: Callable[[CommandContext, str], CommandResult]
```

`CommandContext` exposes the session: `state`, `agent`, `rebuild()`,
`workdir`, `thread_id`, `config` (effective, with session overrides),
`index`, `user_commands`.

```python
@dataclass
class CommandResult:
    text: str = ""                    # Markdown body
    notices: list[Notice] = ...
    choice: Choice | None = None      # title, options [(value, label)], resubmit "/model {value}"
    confirm: Confirm | None = None    # question, resubmit "/undo --yes"
    prompt: str | None = None         # send as a user message
    effects: dict = ...               # e.g. {"model": ..., "provider": ..., "plan": True, "agent_rebuilt": True}
```

Kinds (every REPL command keeps its current behaviour):

| kind | commands |
|---|---|
| `ui` (client-side) | `/help`, `/clear`, `/exit`, `/quit`, `/new`, `/sessions`, `/resume` |
| `read` (allowed mid-turn) | `/usage`, `/diff`, `/context`, `/tools`, `/agents`, `/commands` |
| `mutate` | `/plan`, `/add`, `/drop`, `/undo`, `/redo`, `/model`, `/provider`, `/reload`, `/verify`, `/diagnose`, `/compact` |
| `prompt` | `/init`, user commands from `.luna/commands/` |

- `/model` without an argument → `choice` of the provider's models
  (`model_discovery.list_models`, falling back to `known_models` — the
  discovery half of `choose_model`); when both are empty → an `info`
  notice "текущая модель: X — укажите /model <имя>";
  `/provider` without an argument → `choice` of providers with a key.
- `/undo` → `confirm` unless `--yes` is passed.
- `/init` → `prompt` = `init_prompt(workdir)`; after that turn the agent
  is rebuilt (existing REPL behaviour) via `effects`/auto-reload.
- `/compact` runs synchronously in the request (existing
  `compact_thread` + `undo.forget_messages`).
- Unknown command → `error` notice "неизвестная команда /xyz — /help".
- The server never stores a pending question: choice/confirm are answered
  by re-sending the command with an argument.

### API

- `POST /sessions/{id}/command {workdir, line}` → `CommandResult` JSON.
  During a turn only `read` commands run; others → `409 session_busy`.
  `ui` commands → `400 {"error": "client_command"}`.
- `GET /commands?workdir=` → `[{name, help, kind}]`, built-in + user
  commands.
- `GET /sessions/{id}/state?workdir=` → `{provider, model, plan, pinned,
  usage_summary}` where `usage_summary` is `indicator_line(...)` text.
- All three go through `trust_error` like the other routes.

### REPL adapter

`dispatch(line, ctx)` calls the registry and renders: `text`/`notices`
to the console; `choice` → `arrow_pick` → re-dispatch; `confirm` →
`arrow_confirm` (or the existing `[y/N]` fallback) → re-dispatch with
`--yes`; `prompt` → `DispatchResult(prompt=...)`; `effects` update REPL
state. `ui` commands keep their current REPL implementations.

## 4. Commands in the TUI

- Lines starting with `/`:
  - `ui` handled locally: `/help` (grouped list from `GET /commands`),
    `/clear` (clears the transcript view only), `/exit`/`/quit`, `/new`
    (same as Ctrl+N), `/sessions` and `/resume` (session picker modal
    with type-to-filter, same data as the sidebar), `/resume <n>` opens
    directly.
  - everything else → `POST /command`, rendered in the transcript:
    `text` as a Markdown block, `notices` as dim rows (same widget as the
    `notice` SSE event), `choice` → `ChoiceModal` (↑↓, Enter, Esc cancel)
    then resubmit, `confirm` → `ConfirmModal` (approval-modal style) then
    resubmit, `prompt` → the user line shows `› /init`, the expanded text
    is sent as the message.
  - The command line itself is echoed as a user line.
- The turn runs in a Textual worker so input stays live:
  - `read` commands run immediately mid-turn;
  - other commands and new messages get "Дождитесь окончания ответа";
  - no message queue (out of scope).
- Status bar shows session state from `GET /sessions/{id}/state` (on
  open/switch) and command `effects` (after commands): session model and
  provider, server-side usage, `plan: on` in `$mauve` with the input
  border also `$mauve`, and `pinned: N` when N > 0.
- Autocomplete comes from `GET /commands`, so project commands appear.
- `notice` SSE events render as dim rows under the turn.

## Errors

- A failing command returns `CommandResult` with `error` notices; neither
  server nor TUI crashes (matches current REPL handlers).
- A failed agent rebuild restores the previous session settings.
- `409 session_busy` / `400` / `403` surface as `ServerError` text in the
  TUI.

## Testing

- Registry: each of the 24 commands called directly with a fake runtime,
  asserting its `CommandResult`; current REPL command tests keep their
  scenarios.
- Engine: `prepare_turn` (pinned, `@file`, `@agent`, diagnostics carried
  over) and `finish_turn` / `finish_fixup` (format, diagnose, verify,
  fix-up, reload) with a fake agent.
- Server: two sessions in one workdir are isolated (plan, model, undo
  journal); `409` on a second turn; fix-up turn inside the SSE stream
  including an approval in the middle; `/command` with `choice` /
  `confirm` + resubmit; `GET /commands` includes user commands;
  `GET /state`.
- REPL: `test_repl_flow`, `test_session`, `test_commands` keep passing
  without behaviour changes.
- TUI (pilot): choice and confirm modals, a `read` command mid-turn, a
  `mutate` command mid-turn refused, plan-mode status bar + input border,
  autocomplete with a user command, notices rendered.
- Finish: live pty run, then `critique` and `polish`.

## Out of scope

- Queuing messages during a turn, `/btw`, `/steer`.
- Saving `/model` as a project/global default (`-g`, picker `s`).
- Rewinding conversation together with files (`/rewind`).
- Aborting a running turn from the TUI.
