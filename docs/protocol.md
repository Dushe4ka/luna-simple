# Luna wire protocol

This document specifies **protocol v1**: the HTTP + Server-Sent-Events API a
running Luna server (`luna serve`, implemented by the app server) exposes,
and that every client — the TUI, the REPL, `luna_core.client.LunaClient`,
third-party SDK users, and the ACP/MCP bridges that sit in front of a
session — talks. It is the single source of truth for the shape of a turn
event; the generated JSON Schema (`schema/events.schema.json`) and
`luna_core.protocol.EVENT_FIELDS` are derived from the same table and are
kept in lockstep by `packages/luna-core/tests/test_protocol.py`.

If you are writing a client, read "Endpoints", "SSE framing" and "Event
catalogue", then use `luna_core.client.LunaClient` (or port its ~150 lines to
your language) instead of hand-rolling HTTP calls.

## Purpose and scope

A Luna server owns zero or more **sessions** (conversations backed by an
append-only event log) inside one or more **workdirs** (project
directories). A client creates a session, sends it a message, and consumes
the turn as a stream of JSON events over SSE. Tool calls that need a human
decision pause the stream with an `approval_needed` event; the client
answers out-of-band on a second endpoint and the *same* stream continues.
Sessions can also run **detached from any client** ("background runs"); a
client can attach to one later and replay everything it missed.

This document does not cover the server's process lifecycle, config
discovery, or how `workdir` trust is established — those are
implementation details of the app server (Task 30). It covers only the
wire-visible contract.

## Versioning

- `PROTOCOL_VERSION` is `1`, reported by `GET /health` as `"protocol": 1`.
- **Additive** changes — a new optional field, a new event type, a new
  endpoint — ship without bumping the version. A client must ignore fields
  and events it does not recognise (`validate_event` rejects an *unknown
  event name*, but a client built against a later version may still see
  fields beyond what an older `EVENT_FIELDS` table required).
- **Breaking** changes — removing or renaming a field, changing a field's
  type or meaning, changing an endpoint's method/path/status codes — bump
  `PROTOCOL_VERSION` and are called out in `CHANGELOG.md`.
- A client should check `GET /health`'s `"protocol"` value against the
  version it was built for and refuse to proceed (or degrade) on a mismatch
  it cannot handle.

## Authentication

Every request carries:

```
Authorization: Bearer <token>
```

The token is opaque to the client — it is whatever the server was started
with (a local secret for `luna serve`, a per-user token for a hosted
deployment). There is no token refresh or exchange endpoint in v1; a
rejected token is a `401` with the standard error body (see below) and the
client should prompt for a new one rather than retry.

## Error format

Any endpoint can fail with a `4xx`/`5xx` status and a JSON body:

```json
{"error": "no session s9"}
```

Machine-readable codes the app server uses: `unauthorized` (401),
`workdir_required` / `workdir_not_absolute` / `workdir_mismatch` (400 — the
session belongs to another project) / `workdir_not_trusted` (403),
`session_busy` (409 — a turn or a mutating command is running),
`client_command` (400 — a command the client handles itself).

`LunaClient` raises `luna_core.client.ServerError` with that message (or the
raw response text/reason when the body is not JSON) for every non-2xx
response, including transport failures (`ServerError` wraps
`httpx.TransportError` too, so "server unreachable" and "server said no"
look the same to calling code).

## Endpoints

Every path below is relative to the server's base URL. `workdir` is always
an absolute, trusted project path.

| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/health` | — | `{"ok": true, "version": str, "protocol": 1}` |
| GET | `/sessions` | `?workdir=` | `{"sessions": [{"id", "title", "created", "updated", "group", "relative_time", "running", "background"}]}` |
| POST | `/sessions` | `{"workdir"}` | `{"id"}` |
| DELETE | `/sessions/{id}` | — | `{"deleted": bool}` |
| GET | `/sessions/{id}/messages` | `?workdir=` | `{"messages": [{"role": "user" \| "assistant", "content"} \| {"role": "tool", "name", "args_preview", "ok", "detail"} \| {"role": "notice", "level", "text"}]}` |
| POST | `/sessions/{id}/messages` | `{"workdir", "content", "images"?: [{"media_type", "data"}], "reload_after"?}` | SSE of turn events until `turn_done` / `error` / `cancelled` |
| POST | `/sessions/{id}/approve` | `{"call_id", "decision"}` | `{"ok": true}` (the open turn stream continues) |
| POST | `/sessions/{id}/cancel` | — | `{"cancelled": bool}` |
| GET | `/sessions/{id}/state` | `?workdir=` | status-bar state |
| GET | `/commands` | `?workdir=` | `{"commands": [...]}` |
| POST | `/sessions/{id}/command` | `{"workdir", "line"}` | command result JSON |
| POST | `/sessions/{id}/background` | `{"workdir", "content", "images"?, "reload_after"?}` | run info |
| GET | `/agents` | — | `{"runs": [...], "tasks": [...]}` |
| GET | `/sessions/{id}/events` | `?after=` | SSE: buffered then live events of a background run |

### `GET /health`

Not authenticated gating, but still answered with the bearer header if
sent. No body.

```json
→ GET /health
← 200 {"ok": true, "version": "0.3.0", "protocol": 1}
```

### `GET /sessions`

Lists the conversations of one project, most recently updated first:
top-level sessions that hold at least one user message (subagent,
background-task and fork children, and sessions nobody typed into yet, are
not listed). `group` buckets sessions by local calendar day for UI headers
(`"today"`, `"yesterday"`, `"week"`, `"older"`); `relative_time` is a
pre-rendered label matching the group (`"сейчас"`, `"11м"`, `"3ч"` today,
`"14:02"` yesterday, `"вт"` this week, `"25 сен"` earlier); `running` is
true while a turn is in flight (or waits for an approval); `background` is
true when it runs with no client attached (see `/agents`). An untitled
session shows `"(new session)"`.

```json
→ GET /sessions?workdir=/repo
← 200 {"sessions": [{"id": "s1", "title": "fix the build", "created": 1760000000.0,
                      "updated": 1760000120.0, "group": "today", "relative_time": "2м",
                      "running": false, "background": false}]}
```

### `POST /sessions`

Creates a session rooted at `workdir`. The id is opaque — treat it as an
arbitrary string, not a UUID.

```json
→ POST /sessions {"workdir": "/repo"}
← 200 {"id": "s1"}
```

### `DELETE /sessions/{id}`

Deletes a session and its event log. Deleting an id that does not exist (or
already ran) returns `{"deleted": false}`, not a `404` — delete is
idempotent. A session whose turn is running is refused with `409
session_busy` (cancel it first).

```json
→ DELETE /sessions/s1
← 200 {"deleted": true}
```

### `GET /sessions/{id}/messages`

The rendered transcript, oldest first — not the raw event log. A `"tool"`
entry summarises one finished tool call (`args_preview` is the same
truncated, human-readable string a `tool_started` event carries; `detail`
is the first line of the result). A `"notice"` entry is a run's completion
notice, logged when the run finished with no client attached (a background
run, or a turn whose client disconnected), so a client reopening the session
sees it after the finished transcript. The whole conversation is shown —
compaction only shortens the model's context — except undone turns. An id
the server has never seen is created (empty) in that `workdir`.

```json
→ GET /sessions/s1/messages?workdir=/repo
← 200 {"messages": [
    {"role": "user", "content": "list the repo root"},
    {"role": "tool", "name": "ls", "args_preview": "path=.", "ok": true, "detail": "3 entries"},
    {"role": "assistant", "content": "The repo root has 3 entries."}
  ]}
```

### `POST /sessions/{id}/messages`

Runs one turn. The response is `text/event-stream`: a sequence of turn
events (see "Event catalogue") ending in exactly one of `turn_done`,
`error` or `cancelled` — unless the turn pauses on `approval_needed`, in
which case the *same* response keeps the connection open across the
decision (see "Approval flow"). `images` is a list of
`{"media_type": "image/png" | "image/jpeg" | ..., "data": "<base64>"}`.
`reload_after` asks the agent to be rebuilt once the turn finishes (used
after a config or MCP-server change).

The turn runs in the server, not in the request: the stream only
*follows* it. A client that disconnects merely unsubscribes — the turn
finishes, is logged, and can be replayed with `GET /sessions/{id}/events`.
Each event of the run carries an extra `"index"` (see `/events`), so a
dropped client can resume from the last one it saw. Out-of-turn events of
the session (`background_done` of a `task(background=true)`, an
`approval_needed` asked by a background subagent) are interleaved as they
happen and carry no `index`. A second message while a turn runs is `409
session_busy`; a turn that cannot be prepared (e.g. a broken project
config) streams a single `error` event.

```json
→ POST /sessions/s1/messages {"workdir": "/repo", "content": "look", "images": [], "reload_after": false}
← 200 text/event-stream
data: {"event": "turn_started", "turn_id": "t1", "index": 0}

data: {"event": "text_delta", "text": "Looking...", "index": 1}

data: {"event": "turn_done", "usage": {"input": 120, "output": 8, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost": 0.0003, "stop_reason": "end", "index": 2}

```

### `POST /sessions/{id}/approve`

Answers a pending `approval_needed`. `call_id` is the opaque id the request
carried (see "Approval flow" for its format when it names a subagent's
call). `decision` is the wire form of a `luna_core.policy.Decision` (see
"Decision format"). The call itself only acknowledges receipt — the
decision's effect (more events, or the turn finishing) arrives on the
still-open `/messages` stream (or on `/events`, for a client that
re-attached). `404` when the session is not loaded or nothing is pending
for that `call_id`; `400` for a malformed decision.

```json
→ POST /sessions/s1/approve {"call_id": "c2", "decision": {"type": "approve"}}
← 200 {"ok": true}
```

### `POST /sessions/{id}/cancel`

Asks the running turn to stop. Returns `false` when the session was not
running (not an error) — the stream, if one is open, will still end with a
`cancelled` event once the cancellation is observed.

```json
→ POST /sessions/s1/cancel
← 200 {"cancelled": true}
```

### `GET /sessions/{id}/state`

Status-bar state: current provider/model, plan-mode flag, pinned files, a
pre-rendered usage summary line (thousands of tokens, then the cost when one
is known; `""` before the first model call), how many turns `/undo` can
revert, and whether a turn is running.

```json
→ GET /sessions/s1/state?workdir=/repo
← 200 {"provider": "anthropic", "model": "claude-opus-4", "plan": false,
        "pinned": ["src/main.py"], "usage_summary": "12k in · 3k out · $0.04",
        "undo_depth": 1, "running": false}
```

### `GET /commands`

Built-in and project-defined slash commands, for help text and
autocomplete. `kind` is one of `"ui"` (handled entirely by the client),
`"read"` (safe mid-turn), `"mutate"` (changes session state) or `"prompt"`
(becomes a message to the agent).

```json
→ GET /commands?workdir=/repo
← 200 {"commands": [{"name": "/plan", "help": "toggle plan mode", "kind": "mutate"}]}
```

### `POST /sessions/{id}/command`

Runs a slash command and returns its structured result — never prints
directly. The shape mirrors `luna.commands.base.CommandResult.to_json()`:
`text`, `notices`, an optional `choice` (pick one of several options) or
`confirm` (yes/no), an optional `prompt` (re-submit as a message) and
`effects` (side effects the client should reflect, e.g. a plan-mode flip).

Commands of kind `"mutate"` are refused with `409 session_busy` while a
turn runs; `"read"` commands answer mid-turn.

```json
→ POST /sessions/s1/command {"workdir": "/repo", "line": "/plan on"}
← 200 {"text": "", "notices": [{"level": "info", "text": "plan mode: on"}], "choice": null,
        "confirm": null, "prompt": null, "effects": {"plan": true}}
```

### `POST /sessions/{id}/background`

Starts a turn detached from the calling client: the session keeps running
after this request returns, and any client can attach to it later with
`GET /sessions/{id}/events`. The response is run info, not a stream. The
body takes the same optional `images` / `reload_after` as `/messages`; a
session that already runs is `409 session_busy`.

```json
→ POST /sessions/s1/background {"workdir": "/repo", "content": "run the full test suite"}
← 200 {"session_id": "s1", "label": "run the full test suite", "status": "running",
        "started": 1760000000.0, "finished": null, "error": "", "cost": 0.0, "text": ""}
```

### `GET /agents`

Every background session run and every background subagent task, across
the whole server — for a dashboard of "what's running". `runs` are detached
sessions (`luna_core.background.RunInfo`: `session_id`, `label`, `status`
— `queued`/`running`/`waiting`/`done`/`failed`/`cancelled` —, `started`,
`finished`, `error`, `cost`, `text`); `tasks` are `task(background=true)`
subagent calls (`luna_core.background.TaskInfo`: `handle`, `label`,
`session_id`, `status`, `started`, `finished`, `ok`, `summary`).

```json
→ GET /agents
← 200 {"runs": [{"session_id": "s1", "label": "run the full test suite", "status": "running",
                  "started": 1760000000.0, "finished": null, "error": "", "cost": 0.0, "text": ""}],
        "tasks": []}
```

### `GET /sessions/{id}/events`

Attaches to a background run: `after` (default `-1`, meaning "from the
start") is the last `index` the client already has. The response is
`text/event-stream` — every buffered event with `index > after`, replayed
immediately, followed by live events as they happen, until the run ends or
the connection is dropped. Each event carries an extra `"index"` field (a
monotonic counter per session, across runs) so a client can resume with a
later `after` if the connection drops. After the replay come the session's
out-of-turn approvals still waiting for a decision (background subagents;
no `index`), then — while the run is live — new events, until its final
one. For a session that is not running the response ends after the replay.
A run that died without a final event (an unexpected server-side
exception) is closed with a synthetic `error` (or `cancelled`) event.

```json
→ GET /sessions/s1/events?after=2
← 200 text/event-stream
data: {"event": "text_delta", "text": "...", "index": 3}

data: {"event": "turn_done", "usage": {...}, "cost": 0.0003, "stop_reason": "end", "index": 4}

```

## SSE framing

Streams use the plain SSE `data:` field only — no `event:`, `id:` or
`retry:` lines, and no multi-line `data:` blocks (each event is one
`json.dumps`, on one line). One event is one frame:

```
data: <json>\n\n
```

A client reads line by line, takes lines starting with `data:`, strips the
prefix and surrounding whitespace, and `json.loads`s the rest; blank lines
are frame separators and are otherwise ignored. This is exactly what
`LunaClient._stream` does, and is deliberately simpler than full SSE (no
reconnection ids, no named events) because reconnection is handled instead
by `GET /sessions/{id}/events?after=`, which replays any run — a dropped
`/messages` stream is resumed there, never by re-sending the message.

## Event catalogue

Every event is a JSON object with a string `"event"` field naming it, plus
the fields below. `luna_core.protocol.EVENT_FIELDS` is the machine-readable
version of this table (required fields and their Python types);
`luna_core.protocol.validate_event` checks an object against it and raises
`ValueError` on any mismatch; `schema/events.schema.json` is the same table
as a JSON Schema `oneOf`. All three are generated from, and tested against,
`luna_core.events.to_wire`.

### `turn_started`

The turn began. Fields: `turn_id` (str) — identifies the turn for undo/redo
and for matching it up in the session's event log.

```json
{"event": "turn_started", "turn_id": "t1"}
```

### `text_delta`

A piece of the assistant's answer text, to append to the message being
built. Fields: `text` (str).

```json
{"event": "text_delta", "text": "hi"}
```

### `thinking_delta`

A piece of the model's reasoning (shown dimmed/collapsed by convention).
Fields: `text` (str). (The provider-level event also carries a `signature`
on its last piece for providers that require it on the next request; the
wire form does not expose it — it is replayed internally, not by clients.)

```json
{"event": "thinking_delta", "text": "hm"}
```

### `tool_started`

A tool call started running. Fields: `call_id` (str, opaque — echo it back
verbatim, never parse it), `name` (str), `args` (dict, the raw call
arguments), `args_preview` (str, a short human-readable rendering of
`args` for a transcript line).

```json
{"event": "tool_started", "call_id": "c1", "name": "ls", "args": {"path": "."}, "args_preview": "."}
```

### `tool_finished`

A tool call finished (successfully or not). Fields: `call_id` (str),
`name` (str), `ok` (bool), `detail` (str, the result text or error
message, possibly truncated), `duration` (number, seconds).

```json
{"event": "tool_finished", "call_id": "c1", "name": "ls", "ok": true, "detail": "3 entries", "duration": 0.1}
```

### `approval_needed`

The turn is paused waiting for a decision on one or more pending tool
calls. Fields: `value` (dict) with an `action_requests` list, each item
`{"call_id", "name", "args", "description", "suggested_rule"}`.
`call_id` is an **opaque string** — treat it as a bag of bytes to echo
back on `POST /sessions/{id}/approve`, never parse or construct it.
In particular, when the call comes from a subagent the parent session
adopted, the id the client sees is `"<child_session_id>:<call_id>"`
(colon-joined); the parent `Session.decide` strips that prefix itself to
route the decision to the right child. A client never needs to know this
— it only needs to pass the string back unchanged. `suggested_rule` is a
ready-made permission rule (e.g. `"execute:ls *"`) a client can offer to
persist via `"always"` on the decision.

```json
{"event": "approval_needed", "value": {"action_requests": [
  {"call_id": "c2", "name": "execute", "args": {"command": "ls"}, "description": "ls",
   "suggested_rule": "execute:ls *"}
]}}
```

### `todos`

The agent's plan (as written by its `write_todos` tool) changed. Fields:
`todos` (list of `{"content", "status"}`, `status` one of
`"pending"`/`"in_progress"`/`"completed"`).

```json
{"event": "todos", "todos": [{"content": "x", "status": "pending"}]}
```

### `notice`

A short status line for the user (not part of the transcript). Fields:
`level` (str, one of `"dim"`, `"info"`, `"ok"`, `"warn"`, `"error"`),
`text` (str).

```json
{"event": "notice", "level": "info", "text": "note"}
```

### `usage_delta`

Token usage for the model request that just finished. Fields: `usage`
(dict: `input`, `output`, `cache_read`, `cache_write`, `reasoning`, all
int). A turn can emit several of these (one per model round-trip); sum them
for the turn total, or just use `turn_done`'s `usage`, which already is
that sum.

```json
{"event": "usage_delta", "usage": {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0, "reasoning": 0}}
```

### `compacted`

Older history was summarised to free context room. Fields: `summary`
(str), `removed` (int, how many messages left the model's context — they
are still in the event log, just no longer sent to the model).

```json
{"event": "compacted", "summary": "s", "removed": 3}
```

### `background_done`

A `task(background=true)` subagent call finished while this turn kept
running. Fields: `handle` (str, opaque), `ok` (bool), `summary` (str,
truncated result text). This is distinct from a background *session* run
finishing — that is observed via `/agents` and, if attached, the
`turn_done`/`error`/`cancelled` that ends its own event stream.

```json
{"event": "background_done", "handle": "h", "ok": true, "summary": "ok"}
```

### `reload_requested`

A tool asked for the agent to be rebuilt (e.g. a new MCP server or skill
was registered mid-turn). No fields beyond `"event"`. A client that owns
the agent process should rebuild it once the turn ends (or immediately,
for a server that supports hot reload); `POST /sessions/{id}/messages`'s
`reload_after` flag is how a client pre-arranges this itself.

```json
{"event": "reload_requested"}
```

### `turn_done`

The turn finished normally — the last event of a successful stream.
Fields: `usage` (dict, the turn's total, same shape as `usage_delta`),
`cost` (number or `null` — `null` when no price is known for the model),
`stop_reason` (str, one of `"end"`, `"tool_use"`, `"max_tokens"`,
`"refusal"`).

```json
{"event": "turn_done", "usage": {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0, "reasoning": 0}, "cost": 0.01, "stop_reason": "end"}
```

### `error`

The turn failed — the last event of a failed stream; the session's log
stays consistent (any dangling tool call is patched with a synthetic
"cancelled" result). Fields: `message` (str, human-readable), `kind` (str
— a `luna_core.errors.ProviderError` kind: `"auth"`, `"rate_limit"`,
`"context_length"`, `"bad_request"`, `"server"`, `"network"`, or
`"unknown"`; defaults to `"error"` for a non-provider failure).

```json
{"event": "error", "message": "boom", "kind": "server"}
```

### `cancelled`

The turn was cancelled by the user — the last event of a cancelled
stream. No fields beyond `"event"`.

```json
{"event": "cancelled"}
```

## Approval flow

1. The agent wants to run a tool the policy marks `ask` (or always asks
   for, e.g. `execute`). The `POST /sessions/{id}/messages` stream emits
   `approval_needed` and then goes quiet — the HTTP connection stays open,
   it just has nothing more to send yet.
2. The client shows the pending request(s) to the user and lets them
   decide.
3. The client calls `POST /sessions/{id}/approve` with the request's
   `call_id` (opaque — see `approval_needed` above) and a decision (see
   "Decision format" below).
4. The approve call returns `{"ok": true}` immediately — it only
   acknowledges that the decision was recorded.
5. The *original* `/messages` stream resumes: the tool runs (or is
   skipped/edited per the decision) and streaming continues until the next
   `approval_needed`, or the turn ends in `turn_done`/`error`/`cancelled`.

A turn can pause more than once; a turn can also have several pending
requests at once (batched tool calls) — `action_requests` is a list for
exactly that reason, though today's agent asks one at a time in practice.
If the client's connection to the `/messages` stream drops while a
decision is pending, the turn keeps waiting: the decision can still be
submitted (the session holds it), and the client re-attaches with
`GET /sessions/{id}/events?after=<last index seen>` to see what happens
next — every turn runs in the server, streamed or not.

## Cancellation

`POST /sessions/{id}/cancel` asks the running turn to stop at the next safe
point (between tool calls, or by interrupting the model stream). It
returns `true` if a turn was running, `false` if the session was idle —
either way it is safe to call. The in-flight `/messages` stream (if any)
ends with a `cancelled` event; any tool call left dangling gets a synthetic
"cancelled" result in the session's log so the transcript (and the next
turn's context) stays consistent. Cancelling also resolves any pending
approval the turn was waiting on.

## Background runs

A session can run a whole turn **detached from any client**:

- `POST /sessions/{id}/background` starts it and returns immediately with
  run info (`status: "queued"` moving to `"running"`); no client needs to
  stay connected.
- `GET /agents` lists every such run (plus subagent background tasks) on
  the server, for a "what's in flight" view.
- `GET /sessions/{id}/events?after=` attaches to one run's event stream:
  every event the run has produced so far with `index > after` (default
  `-1`, i.e. everything), replayed first, then live events, ending the
  same way a normal turn would (`turn_done`/`error`/`cancelled`). Any
  number of clients can attach at once; none of them owns the run, so
  disconnecting never cancels it — only `POST /sessions/{id}/cancel` does.
- Each buffered/live event carries an extra `"index"` (monotonic per
  session, across every run that session has had) so a client that drops
  and reconnects can resume exactly where it left off by passing the last
  `index` it saw as the new `after`.
- A finished run's info and buffered events are kept for a bounded number
  of most-recently-finished runs (server-side retention), after which an
  old run's id answers `GET /sessions/{id}/events` as if it never
  happened — clients should not rely on indefinite replay.

`LunaClient.background()` starts one; `LunaClient.agents()` lists them;
`LunaClient.attach(session_id, after=...)` replays and follows one.

## Decision format

A decision answers one `approval_needed` request. Wire form (see
`luna_core.policy.Decision.to_dict` / `.from_dict`):

| `type` | Extra fields | Meaning |
|---|---|---|
| `"approve"` | `"always"`? (str, a permission rule) | Run the call as proposed. `always`, when given, persists the rule so future matching calls skip approval. |
| `"edit"` | `"args"` (dict) | Run the call with these arguments instead of the ones proposed. |
| `"reject"` | `"message"`? (str) | Do not run the call; the model sees `message` as the tool's result (empty string if omitted). |

```json
{"type": "approve"}
{"type": "approve", "always": "execute:ls *"}
{"type": "edit", "args": {"command": "ls -la"}}
{"type": "reject", "message": "not needed"}
```

`LunaClient.decide(session_id, call_id, decision)` accepts either a
`luna_core.policy.Decision` instance or an already-wire dict.

## See also

- `schema/events.schema.json` — the same event table as a JSON Schema
  (draft 2020-12, `oneOf` of one variant per event), generated by
  `python -m luna_core.protocol` and kept in sync by
  `packages/luna-core/tests/test_protocol.py`.
- `luna_core.protocol` — `PROTOCOL_VERSION`, `EVENT_FIELDS`,
  `validate_event`, `json_schema`.
- `luna_core.client.LunaClient` — the reference client for this API; see
  `packages/luna-core/src/luna_core/client.py` and its tests
  (`packages/luna-core/tests/test_client.py`) for a runnable example of
  every call in this document.
- `luna_core.policy.Decision` — the decision type `LunaClient.decide`
  accepts.
- `luna_core.events.to_wire` — produces every event this document
  describes; it is the actual source of truth, this document and the
  schema are its specification written down.
