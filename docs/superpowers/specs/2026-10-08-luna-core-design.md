# luna-core: our own agent runtime + SDK, and leaving deepagents / LangChain / LangGraph

Date: 2026-10-08
Status: approved in conversation, awaiting written-spec review

## Goal

Replace deepagents, LangChain and LangGraph with our own Python runtime,
`luna-core`, which is also a public SDK for embedding the Luna agent in other
projects, and move the Luna app (CLI, REPL, TUI, server, commands) onto it.
Everything core-related that was previously labelled "later" is part of this
plan: native Google API, OpenAI Responses API, GigaChat, user hooks, a Docker
sandbox, and ACP for editors. No framework package remains installed at the
end.

Stars (always-on agents) and the web/desktop client are separate products with
their own specs and plans, built on this core afterwards.

## Decisions (from the conversation)

- Language stays **Python** (3.11+).
- Architecture: own async runtime, own tools, own event-log session store,
  thin provider adapters, official protocol SDKs only for MCP and ACP.
- **No LiteLLM.** Analogues that lead the market write their own adapters
  (pi-ai: four wire APIs cover ~816 of 999 catalogued models; Hermes: own
  adapters + OpenAI-compatible path; Codex, DeepSeek Harness: own; opencode:
  Vercel AI SDK). LiteLLM was compromised on PyPI on 2026-03-24 (1.82.7–1.82.8
  stole SSH keys and cloud secrets via a `.pth` file), is a heavy dependency
  (light install is a launch P0), and hides provider differences we must
  control (reasoning blocks, caching, tool-call formats). A LiteLLM *proxy*
  still works as an ordinary OpenAI-compatible provider, with no code of ours.
- Provider adapters use **plain `httpx` + `httpx-sse`**, no vendor SDKs.
- **Clean slate for sessions:** old LangGraph checkpoints are not migrated
  (only test sessions exist; users get the new core from day one).
- **No dual-engine switch:** the core is built and tested alongside the app,
  then the app is cut over in one task and the frameworks are removed.

## Current state (verified 2026-10-08)

- Startup: `import luna.cli` 1.3 s (python itself 0.02 s), of which
  `import deepagents` 0.7 s. 140 packages / 244 MB with all extras.
- deepagents: 27 463 lines, 88 releases in 12 months, pinned at 0.7.13
  (latest 0.7.23). Framework imports in 14 Luna files (2 850 lines);
  23 of 69 test files use LangChain types.
- Coupling points: `create_deep_agent` (`luna/core/agent.py`), the turn event
  adapter `iter_turn` (`luna/core/turn_events.py`), checkpointer
  (`luna/core/persistence.py`), approvals via `interrupt`/`Command`
  (`luna/server/turns.py`, `approvals.py`, `luna/core/session.py`), message
  types in undo/history, `@tool`, MCP adapter, `init_chat_model`.
- Base request overhead today: system prompt ~530 tokens + 14 tool schemas
  ~3.9 k tokens.
- Behaviour deepagents provides that must be reproduced (inventoried from
  `deepagents/graph.py` 0.7.13): filesystem tools `ls`, `read_file`
  (offset/limit, default limit 100 lines, char budget, media), `write_file`,
  `edit_file` (old/new string, `replace_all`, uniqueness), `delete`, `glob`,
  `grep` (`path`, `glob`, `output_mode`, `max_count`), `execute` (`timeout`);
  `task` subagents incl. a default general-purpose subagent; summarization of
  old history when the context fills up (evicted history offloaded to the
  backend); `PatchToolCallsMiddleware` (dangling tool calls); Anthropic prompt
  caching; memory (AGENTS.md + memory files) and skills (index + on-demand
  `SKILL.md`) in the system prompt; human-in-the-loop on configured tools with
  approve / edit / reject; large tool-result eviction / overflow clipping.

## 1. Packages

uv workspace in this repo:

```
packages/luna-core/      import name luna_core   (PyPI: luna-core)
  src/luna_core/...
  tests/
luna/                    the app                 (PyPI: luna-simple)
```

- `luna-core` dependencies: `httpx`, `httpx-sse`, `pyyaml`.
  Extras: `[mcp]` → `mcp`; `[acp]` → `agent-client-protocol`;
  `[docker]` → nothing (uses the `docker` CLI); `[lsp]` → `multilspy`;
  `[websearch]` → `ddgs`.
- `luna-simple` depends on `luna-core` plus its UI/server deps (`rich`,
  `textual`, `starlette`, `sse-starlette`, `uvicorn`, `questionary`).
- Public API is everything exported from `luna_core/__init__.py`; it follows
  semver from 0.1.0. Internals live under `luna_core._internal` or are
  documented as private.

## 2. Core modules (`luna_core`)

### messages, events
Own dataclasses (no pydantic in core):
- Content blocks: `Text`, `Thinking` (with provider signature for round-trip),
  `Image` (base64 + media type), `ToolCall(id, name, args)`,
  `ToolResult(call_id, content blocks, is_error)`.
- Messages: `System`, `User`, `Assistant`, `ToolResults`; JSON
  (de)serialisation with a schema version.
- Model stream events: `TextDelta`, `ThinkingDelta`, `ToolCallStart/Delta/End`,
  `Usage(input, output, cache_read, cache_write, reasoning)`, `Stop(reason)`.
- Turn events (public, also the wire protocol): `TurnStarted`, `TextDelta`,
  `ThinkingDelta`, `ToolStarted(call_id, name, args, preview)`,
  `ToolFinished(call_id, ok, detail, duration)`, `NeedsApproval(request)`,
  `Notice(level, text)`, `UsageUpdate`, `Compacted`, `TurnDone(usage, cost)`,
  `TurnError(message)`, `Cancelled`.

### providers
- `Provider` protocol: `async def stream(request: ModelRequest) ->
  AsyncIterator[ModelEvent]`; `ModelRequest(model, system, messages, tools,
  max_tokens, temperature, reasoning, cache)`.
- Adapters:
  - `openai_chat` — Chat Completions (OpenAI, DeepSeek incl.
    `reasoning_content`, Groq, Together, Fireworks, OpenRouter, Ollama, xAI,
    Cerebras, Mistral, Perplexity, Yandex AI Studio, custom endpoints).
  - `openai_responses` — Responses API (OpenAI reasoning models, encrypted
    reasoning round-trip).
  - `anthropic_messages` — Messages API with `cache_control` breakpoints,
    thinking blocks with signatures, tool use.
  - `google_genai` — native Gemini `streamGenerateContent` (function
    declarations, thought parts, thought signatures).
  - `gigachat` — GigaChat REST: OAuth token exchange with refresh,
    `functions` format, SSE; configurable CA bundle (Russian root CA).
- Shared: retries with backoff on 429/5xx/timeouts (respect `Retry-After`),
  connect/read timeouts (no read timeout on streams), error mapping to
  `ProviderError(kind=auth|rate_limit|context_length|bad_request|server|network)`,
  usage normalisation, cost from the model catalogue (`models.toml` moves to
  the core), context-window lookup.
- `providers.resolve(provider_key, model, config)` builds an adapter from the
  provider registry (built-ins + custom OpenAI-compatible providers).

### tools
- `@tool` decorator: JSON Schema from type hints + docstring (`str`, `int`,
  `float`, `bool`, `list[...]`, `Literal[...]`, `Optional`), sync or async.
- `ToolContext`: workspace, sandbox, session, policy, cancellation, emit
  progress.
- Built-ins with today's names and parameters (so prompts/skills keep
  working): `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`,
  `grep` (ripgrep when available, Python fallback), `execute`, `task`.
  Descriptions are rewritten by us, shorter (target: all built-in schemas
  ≤ 2 k tokens).
- Independent tool calls in one assistant message run concurrently (reads in
  parallel; mutating calls serialised per path).
- Results over a size budget are written to `.luna/tool-output/<id>.txt` and
  replaced by a head/tail excerpt + path.

### workspace, sandbox
- `Workspace(root)`: resolves agent paths; a path outside the root is an
  explicit tool error ("outside the project: …"), never silently remapped.
- `Sandbox` protocol: `run(command, timeout, env) -> (exit, output)`.
  - `LocalSandbox` (default).
  - `DockerSandbox`: one container per session (`docker run -d` with the
    workdir mounted at `/workspace`, `docker exec` per command), image and
    network (`none` | `bridge`) configurable, container removed when the
    session closes.

### policy
Permission rules (allow/deny/ask, moved from `luna/core/permissions.py`),
plan mode, the edit-anchor tracker, undo snapshots before mutating tools,
trust check hook. Approvals: a tool that needs one makes the loop emit
`NeedsApproval` and await `session.decide(call_id, decision)`; decisions
`approve`, `edit(new_args)`, `reject(message)`, `always(rule)`.

### hooks
Points: `session_start`, `pre_tool`, `post_tool`, `turn_end`, `pre_compact`.
- Python hooks (SDK users and the Luna app): async callables that can
  modify args, block with a message, or add notices.
- User command hooks from config (`[[hooks]]` in `.luna.toml` / global
  config): `event`, optional `match` (tool name glob), `command`, `timeout`.
  The hook gets a JSON payload on stdin; exit 0 = continue (stdout may add
  context), exit 2 = block (stderr is returned to the model), other = warning.
- Luna's verify / format / diagnose pipeline (today `luna/turn/engine.py`)
  becomes `post_tool` + `turn_end` hooks in the app.

### context
- System prompt assembly: base prompt + AGENTS.md + memory files + skills
  index + tool-use guidance; cache breakpoints placed by the adapter.
- Compaction: when estimated tokens exceed 80% of the model's context window,
  the oldest messages (keeping the last N turns intact and never splitting a
  tool call from its result) are summarised by the model into a
  `Compacted(summary)` event; full history stays in the log.
- Dangling tool calls (assistant tool calls without results after a crash or
  cancel) get synthetic "cancelled" results before the next request.

### session (event log)
- SQLite table `events(session_id, seq, ts, type, payload_json,
  schema_version)`, plus `sessions(id, workdir, title, created, updated,
  parent_id, fork_seq)`.
- `Session.messages()` rebuilds the model context from events (respecting
  compactions); `fork(at_seq)`; `rewind(to_seq)` (conversation; files via
  undo snapshots); `export(fmt="md"|"html"|"json")`.
- Concurrency: one writer per session, WAL mode, short transactions.
- The Luna app's existing tables (`luna_sessions`, `luna_projects`,
  `luna_session_state`) are folded in: projects + per-session settings stay,
  `luna_sessions` is replaced by `sessions`.

### loop
`Agent(model, tools, system, policy, sandbox, hooks, max_steps, subagents)`:
- `async for event in agent.run(session, input)` — input is text and/or
  image blocks.
- Steps: hooks → build request → stream model → execute tools (approvals,
  hooks, concurrency) → append events → repeat until the model stops or
  `max_steps`.
- Cancellation: cancelling the consumer task cancels the in-flight request
  and tools, records `Cancelled`, patches dangling calls.
- `agent.run_sync(...)` wrapper for non-async callers.
- Message queue: `session.queue(text)` delivers user text at the next step
  boundary (`/steer`); `/btw` runs a side request without touching history.

### subagents, skills, memory, mcp, usage
- Subagents: `task(description, subagent_type)` runs a child `Agent` with a
  fresh session branch, inherited policy/sandbox/hooks, own tool subset and
  model; definitions from `subagents.toml`; a default general-purpose one.
  Children run concurrently when the model issues several `task` calls.
- Skills: SKILL.md with YAML frontmatter (safe-dumped, validated), index in
  the prompt, body read on demand; `save_skill` keeps working.
- Memory: AGENTS.md + `.luna/memory/*.md` tiers; `remember` tool.
- MCP (extra): stdio and streamable-HTTP servers via the official `mcp`
  `ClientSession`; tools adapted to our `Tool`; OAuth for HTTP servers via
  the SDK's auth provider.
- Usage: per-turn and per-session tokens and cost, budget (`max_cost`)
  enforced by the loop.

### parallel and background agents
Built on the async loop; scoped to a project (not Stars):
- **Parallel subagents:** several `task` calls in one assistant message run
  concurrently (already above). Each can run in an **isolated git worktree**
  (`isolation="worktree"`, default for subagents allowed to write) so parallel
  edits never collide; the parent gets a summary plus the branch/diff to merge.
- **Background subagents:** `task(..., background=true)` returns a handle at
  once; the parent continues. When the child finishes, a
  `BackgroundTaskDone(handle, summary)` event is appended and its result is
  delivered to the parent at the next step boundary. Tools `task_status`,
  `task_cancel`.
- **Background sessions:** a whole agent session can run detached from any
  UI — `luna run --background "…"` (CLI), "send to background" in the TUI,
  `POST /sessions/{id}/background` on the server. `AgentManager` (in the
  server process) owns running sessions: concurrency limit, cancel, status,
  completion notices; sessions survive the TUI closing because they live in
  the server.
- **UI:** `/agents` lists running/finished background work with status, cost
  and a way to open its transcript; the sidebar marks sessions that are
  running in the background; completion shows a notice/toast.
- Limits: `max_parallel` (default 4), per-agent `max_cost` and `max_steps`;
  the global budget is checked before spawning.

### model roles
`roles = {default, fast, plan, vision, summarize}` → model ids; compaction
uses `summarize`, subagents default to `fast`, plan mode uses `plan`, image
input falls back to `vision` when the default model lacks it.

### client, protocol, acp
- `luna_core.client`: async HTTP/SSE client for a running Luna server
  (moved from `luna/server/client.py`), typed with the turn events.
- `docs/protocol.md` + `schema/events.schema.json`: the wire format of turn
  events and command results, versioned.
- `luna_core.acp` (extra): an ACP agent over stdio (`luna acp`) mapping
  `new_session`/`prompt`/`cancel` to sessions and the loop, turn events to
  `session_update` (message/thought chunks, tool_call, tool_call_update,
  plan, usage), approvals to `request_permission`.

## 3. The Luna app on the core

- `luna/core/agent.py` → builds a `luna_core.Agent` from `LunaConfig`.
- `luna/core/session.py` (REPL) and `luna/server/turns.py` consume
  `agent.run(...)` events; approvals answered via `session.decide`.
- `luna/server/runtime.py` holds a core `Agent` + `Session` per session;
  its threadpool workarounds go away (the core is async).
- Commands registry keeps its API; `/compact`, `/undo`, `/redo`, `/rewind`,
  `/export`, `/btw`, `/steer` are implemented on the core session.
- TUI: Esc / Ctrl+C cancels the turn; image paste/attach (`@image.png` and
  clipboard paste of a file path); the status-bar undo counter shows the real
  depth.
- `luna acp` subcommand; `luna sessions rm|prune`.
- Background work: `luna run --background`, "send to background" in the
  TUI, `/agents`, background markers in the sidebar, completion notices.
- Removed from dependencies: `deepagents`, `langchain*`, `langgraph*`,
  `langchain-mcp-adapters`, all `langchain-<provider>` extras.

## 4. SDK

1. Embedded: `from luna_core import Agent, Session, tools, providers`.
2. Remote: `luna_core.client.LunaClient` against `luna serve`.
3. Protocol: documented JSON event schema + ACP.
4. `examples/`: minimal agent, custom tool, custom provider, approvals in a
   web app, remote client, ACP.
5. Docs: `packages/luna-core/README.md` + `docs/sdk/*.md` (quickstart,
   concepts, API reference generated from docstrings).

## Errors

- Provider errors surface as `TurnError` with a readable message; auth
  errors name the env var / `luna config set-key`.
- Tool errors are tool results (`is_error`), never exceptions out of the loop.
- Hook failures are notices; a hook timeout never blocks a turn forever.
- A crash mid-turn leaves a consistent log; the next turn patches dangling
  calls.

## Testing

- Core unit tests with `FakeProvider` (scripted model events).
- Adapter tests on recorded fixtures (`httpx.MockTransport`, JSON + SSE) for
  each wire API: text, tool calls, parallel tool calls, reasoning, usage,
  errors, retries. `pytest -m live` runs against real keys (manual/nightly).
- Contract suite every adapter must pass (same scenarios, all providers).
- Loop/policy/session/compaction/subagent/hook/sandbox tests in the core,
  plus parallel subagents in worktrees, background tasks (handle, status,
  cancel, delivery) and background sessions surviving a client disconnect.
- The app's existing suite ported to core types is the acceptance gate.
- Targets: `luna --version` < 150 ms; TUI first frame < 0.4 s; built-in tool
  schemas ≤ 2 k tokens; "find and fix a bug" passes on 3 real repositories
  with DeepSeek and Anthropic; `uv pip list` shows no `langchain*`,
  `langgraph*`, `deepagents`.

## Out of scope (separate specs and plans)

- Stars (always-on agents, scheduling, channels).
- Web and desktop clients.
