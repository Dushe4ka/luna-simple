# Luna TUI (Server + Full-Screen Client) — Design

## Purpose

The last remaining "in the perspective" roadmap item from the 2026-09-21
competitive-analysis session: a custom, polished full-screen terminal UI for
Luna, "in Luna's own style." During brainstorming this grew from "build a
TUI" into "extract Luna's session/agent core into a server with a protocol
that survives the TUI" — because the user's actual long-term goal (also
named in that original roadmap as a separate deferred item) is multi-channel
access to the same running Luna session: TUI now, web/Telegram/mobile later,
all against the same backend.

## Research That Shaped This Design

Checked directly, not assumed, during brainstorming (2026-09-23/24):

- **Textual** (Textualize, authors of `rich`, which Luna already depends
  on — `textual` itself is confirmed NOT currently installed, so this is
  a genuinely new dependency, unlike the server-side libraries below):
  production-stable (`Development Status :: 5 - Production/Stable`),
  actively maintained, sponsored by Anthropic/Meta/NVIDIA/Microsoft/Bloomberg.
  The default choice for a new full-screen Python terminal app in 2026.
  Known caveat: input-latency reports when run inside `tmux` (escape-key
  callbacks >1s vs instant in a bare terminal) — noted as a real, if narrow,
  risk to test against, not disqualifying.
- **Aider** (the closest Python peer — a terminal coding agent): does NOT
  build a full-screen TUI. It layers `prompt_toolkit` (multiline input,
  history, completion) on top of `rich` output, in the same line-scrolling
  paradigm Luna already uses. Rejected here because the user explicitly
  wants the full-screen "wow" register (k9s/lazygit-style), not an
  upgraded line editor — but this was a real, considered alternative, not
  a strawman.
- **OpenCode**: full-screen TUI, but a JS/Zig stack (`opentui`), not
  Python-portable. Architecturally useful as a *reference pattern*, which
  this design follows: OpenCode's TUI is a **thin client** — a separate
  process talking to a session/agent server over REST + Server-Sent
  Events, handling only presentation and ephemeral UI state (theme,
  scroll position), with all business logic server-side. Luna's TUI
  copies this shape for the same reason OpenCode has it (multi-client
  future) and because it cleanly separates "what changed" (session logic,
  unchanged) from "how it's shown" (new).
- **Streaming markdown without re-parse jank**: Textual ships
  `MarkdownStream`, built specifically for streaming LLM output into a
  terminal (documented by Textualize's founder). It re-parses only the
  most recent unclosed block (not the whole document on every token),
  keeping parse cost sub-1ms regardless of response length, with a buffer
  absorbing bursts of fast-arriving tokens. This resolves what looked
  like a trade-off (live streaming vs. jank-free markdown) — both are
  achievable via this one library feature, not a compromise.
- **Palette contrast, checked against `ui-ux-pro-max`'s guidance** (per
  the user's global CLAUDE.md instruction — domains `ux` and `color`
  queried directly, not applied from memory): computed real WCAG contrast
  ratios for the existing `luna/ui/theme.py` `PALETTE` against its `bg`
  (#0b1026). `blue` (#5566a8) — used informally in this design's mockups
  as "dim" persistent text (sidebar labels, timestamps) — measures
  **3.45:1**, below the skill's flagged 4.5:1 minimum for normal text.
  `moon_dim` (#aab4e8) measures **9.32:1**. In the current scrolling REPL
  this barely matters (a dim line scrolls past); in a full-screen TUI,
  sidebar labels and status-bar text sit on screen persistently, so this
  design uses `moon_dim` for persistent secondary text and reserves
  `blue` for decorative/structural elements (borders, less-critical
  accents) where the lower contrast is acceptable. No other palette
  token falls below 6.8:1.

## Success Criteria

- `luna` (no flags) launches the new full-screen TUI by default. Existing
  headless mode (`luna "..." --json`) is untouched — it keeps calling
  `luna/core/session.py`/`agent.py` directly, in its own process, with no
  server involved, exactly as today.
- The TUI never re-implements session/agent/approval/undo logic — it is a
  presentation layer over the existing `luna/core/*` code, unchanged,
  now reached through a local HTTP+SSE server instead of an in-process
  REPL loop.
- A user can close the TUI (or the terminal) mid-session and reopen
  `luna` later; the server (if still running) is reused and the session
  resumes with its state intact — the same `--resume`/SQLite persistence
  Luna already has, just decoupled from any one client process's
  lifetime.
- The protocol (REST + SSE) is shaped so that a *future* client (web,
  Telegram, mobile) can be built against the *same* server without a
  protocol redesign — this plan does not build those clients, but does
  not paint the protocol into a TUI-only corner either.
- Approval prompts, `/plan` mode, tool-call progress, subagent activity,
  and session resume all have first-class TUI representations — not
  degraded relative to the current REPL.
- Old `luna/repl/*` line-oriented REPL code is left in the codebase,
  unmaintained but not deleted, during this plan. Its removal is an
  explicit future decision, out of this plan's scope.

## Architecture

```
┌─────────────────┐   REST + SSE    ┌──────────────────────────────┐
│   TUI client     │◄───(local       │        Luna Server            │
│  (Textual app,   │   socket/       │  (new process; auto-started   │
│   asyncio)       │───127.0.0.1)───►│   by the TUI if not running,  │
└─────────────────┘                 │   or via `luna serve`)        │
                                     │                                │
                                     │  ┌──────────┬──────────────┐  │
                                     │  │session.py│  agent.py    │  │
                                     │  ├──────────┼──────────────┤  │
                                     │  │toolguard │  undo.py     │  │
                                     │  │  .py     │              │  │
                                     │  └──────────┴──────────────┘  │
                                     │  (existing, unmodified logic) │
                                     └──────────────────────────────┘
┌─────────────────┐  direct in-      ▲
│  Headless CLI    │  process call   │
│ (luna "..." --json)──(no server)───┘  same session.py/agent.py,
└─────────────────┘                    separate process, as today

Future, separate cycles, same protocol, not this plan:
  [Web]  [Telegram]  [Mobile]  ──── REST + SSE ────►  same Luna Server
```

### Server (new: `luna/server/`)

A thin ASGI wrapper — **`starlette` + `sse-starlette` + `uvicorn`**,
confirmed already present as transitive dependencies in this project's
resolved environment today (`uv pip list` shows `starlette 1.6.0`,
`sse-starlette 3.4.11`, `uvicorn 0.52.4`, `httpx-sse 0.4.3` — no new
dependency needed for the server layer; the client side already has
`httpx` as a direct dependency, and `httpx-sse` gives it a ready-made SSE
consumer too). `sse-starlette` in particular removes the need to
hand-roll SSE framing. This is now a settled choice, not deferred to the
plan — around one long-lived
`luna.core.session`/`agent.py` instance per open session. Responsibilities:

- Accept a new turn (`POST /sessions/{id}/messages`), return an SSE stream
  of the same events the current REPL already renders inline: text
  chunks (for `MarkdownStream`), tool-call started/finished, an approval
  request (blocking, mirroring today's `__interrupt__` payload), plan-mode
  state changes.
- Accept an approval decision (`POST /sessions/{id}/approve`) — the exact
  `Command(resume={"decisions": [...]})` shape `luna/core/session.py`
  already builds, just arriving over HTTP instead of a blocking `input()`.
- List/create/resume sessions (`GET /sessions`, session auto-naming
  happens server-side once, after the first user message, so every
  future client sees the same name — not recomputed per-client).
- Bind to a local Unix socket or `127.0.0.1` with a token written to
  `~/.config/luna/` (mode 0600, matching the existing `credentials.toml`
  convention) — no user-facing auth flow needed for the local-only case
  this plan covers.

### TUI client (new: `luna/tui/`)

A Textual `App` subclass. Talks to the server exclusively through the
protocol above — no direct import of `luna.core.*` from this package
(mirrors the existing `AGENTS.md` convention of confining framework
imports to specific files, extended here: the TUI package should not
import `deepagents`/`langgraph` at all, only an HTTP/SSE client).

**Layout** (approved via mockup): three zones, collapsible via `Ctrl+B`.
- **Left sidebar**: session list. Each entry shows an auto-generated name
  (derived from the session's first message, computed server-side) and a
  relative last-activity timestamp ("2м", "вчера", "18 сен").
- **Center**: the chat transcript, rendered via Textual's `Markdown`
  widget + `MarkdownStream` for live token-by-token updates without
  re-parse jank (see Research above). Input box at the bottom, with
  slash-command autocomplete: typing `/` opens a filtered dropdown of
  available commands (matching substring highlighted), narrowing as the
  user types further (`/cle` → `/clear`, `/cleanup`); arrow keys move the
  selection, Tab/Enter accepts.
- **Right sidebar**: live tool-call activity (the pulsing in-flight
  indicator, ported from `luna/ui/progress.py`'s existing logic/visual
  language, now server-driven via SSE events instead of an in-process
  callback).
- **Status bar** (bottom): model, running cost, active `@file` context,
  `/plan` state, undo-journal depth — the detail that used to live in
  separate always-visible sidebar sections now collapses into one line,
  per the "informative but not cluttered" direction from mockup review.
- **Approval**: a genuine Textual `ModalScreen`, replacing the current
  blocking `rich.Panel` in `luna/ui/approve.py` — same decision options
  (approve/edit/always/reject), same diff/command preview content, new
  presentation.

### Visual design

Reuses `luna/ui/theme.py`'s `PALETTE` (translated into Textual's CSS-like
styling) — the existing moon/night identity, not a redesign. One
adjustment: persistent secondary text (sidebar labels, timestamps, status
bar) uses `moon_dim` (9.32:1 contrast) instead of `blue` (3.45:1),
per the contrast check in Research above. `blue` remains for borders and
lower-emphasis structural elements.

## Data Flow (one turn, TUI client)

```
user types in TUI input box
  → POST /sessions/{id}/messages {"content": "..."}
  → server: session.py runs the existing turn logic against agent.py
  → SSE stream back to TUI:
      event: text_delta      (fed into MarkdownStream)
      event: tool_started    (right sidebar activity entry appears)
      event: tool_finished   (activity entry resolves)
      event: approval_needed (TUI opens the ModalScreen; blocks server-side
                               exactly like today's __interrupt__ does,
                               until...)
  → user decides in the modal
  → POST /sessions/{id}/approve {"decision": "approve"}
  → server resumes the graph via the existing Command(resume=...) path
  → SSE stream continues (more text_delta / tool_* events) until the turn ends
```

## Error Handling

Server-side failures (a crashed agent turn, a broken tool) surface as a
distinct SSE `error` event the TUI renders inline in the transcript,
matching how the current REPL's broad `except` in the turn loop already
reports failures — no new failure taxonomy invented. A TUI that can't
reach the server (not started, crashed) shows a clear connection-status
indicator rather than hanging silently; reconnection to an already-running
server (e.g., after the TUI process itself is killed and relaunched)
re-attaches to the existing session list without re-running anything.

## Testing

- **Server**: tested as an ASGI app via an in-process test client (e.g.
  `httpx.AsyncClient` against the app object) — no real socket, no
  network, matching this project's existing "tests never touch the
  network" convention. `FakeToolCallingModel` (already used throughout
  the test suite) drives the underlying agent exactly as today's
  `test_agent.py` tests do; only the transport (HTTP+SSE vs. direct
  Python call) is new.
- **TUI**: Textual's own `Pilot`/snapshot-testing tooling (the standard,
  documented way to test Textual apps — simulates key presses and mouse
  events against a running app instance, asserts on rendered output),
  run against the real server test client from above, not mocks.

## Out of Scope

- The web, Telegram, and mobile clients themselves — this plan builds
  only the server + protocol shape that would let them exist later,
  each as its own future brainstorm→spec→plan cycle.
- Network-facing authentication beyond a local token file — this design
  covers `127.0.0.1`/local-socket use only.
- Removing or actively maintaining the old `luna/repl/*` line-oriented
  REPL — left in place, unmaintained, for this plan; its fate is a
  separate future decision.
- Setup wizard's exact placement (inside the TUI's first-run flow, or
  still a separate pre-TUI step) — resolved at the plan level.
- Auto-generated session naming's exact mechanism (a small dedicated LLM
  call vs. a heuristic truncation of the first message) — resolved at
  the plan level; both keep the server-side-computed-once property this
  spec requires.
