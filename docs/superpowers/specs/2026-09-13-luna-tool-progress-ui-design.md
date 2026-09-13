# Luna Tool-Progress UI — Design

## Goal

While an agent turn runs, Luna currently shows nothing for a tool call
until it has already finished (a single dim line with the tool name and
the first line of its result), and shows literally nothing at all while a
delegated subagent (`task`) runs — the user just watches a blank gap,
possibly for tens of seconds. This spec adds a live, animated progress
indicator for in-flight tool calls — including subagent delegations — and
gives each tool type an informative one-line "what is this call actually
doing" summary, modeled on the two-line pattern real competitor CLIs
(Claude Code and others, confirmed by direct inspection of the
already-installed `deepagents` package and this repo's own REPL loop) use,
but restyled in Luna's own palette and animation language (the same
gradient/pulse craft already used for the splash screen).

## Background: what the runtime actually gives us

Luna's `_stream_turn` (`luna/core/session.py`) drives one turn via
`agent.stream(payload, config=config, stream_mode=["messages", "updates"])`.
Two facts about this streaming shape drive every design decision below:

1. **A tool call's start is visible before it finishes.** In `updates`
   mode, when the model's node finishes producing an `AIMessage`, that
   `AIMessage.tool_calls` is already fully populated (name, args, id) —
   this happens in a separate `updates` chunk *before* the tool actually
   runs and *before* its `ToolMessage` result shows up in a later chunk.
   Today's code only reacts to the `ToolMessage` (`_report_tools`); it
   never looks at the `AIMessage` that requested the call. That is the
   gap this spec closes: we now also react to the tool-call-*requested*
   event, not only the tool-call-*completed* event.
2. **A subagent's internal steps are invisible — permanently, not just
   until we add code.** `deepagents`' `task` tool (confirmed by reading
   `middleware/subagents.py` in the installed package) runs the subagent
   with a plain, blocking `subagent.invoke(subagent_state, subagent_config)`
   call *inside* the tool function. That call is not part of the parent
   graph's own node stream — it is a synchronous nested call, so nothing
   the subagent does internally (its own tool calls, its own model
   tokens) ever reaches `agent.stream()`'s `updates`/`messages` output.
   Getting genuine live visibility into a subagent's internal steps would
   require an entirely separate mechanism (a LangChain callback handler
   threaded through the subagent's config, consumed across a thread
   boundary since `invoke()` blocks). That is out of scope for this spec
   — confirmed with the user, who chose the lightweight option: treat
   `task` as one long-running opaque call with a spinner and an elapsed
   timer, exactly the granularity Claude Code itself actually shows for
   its own subagents.

Both facts mean the design below needs no new hooks into `deepagents` or
`langgraph` internals — only new handling of data already flowing through
`_stream_turn`'s existing `updates` stream, on both ends (call-requested,
call-completed).

## Non-goals

- No live view into a subagent's internal tool calls (see above).
- No live/incremental Markdown rendering of the assistant's answer text —
  real risk of flicker or corruption at chunk boundaries (a `**` split
  across two streamed tokens, a code fence still open) for benefit the
  user did not specifically ask for. The answer text keeps streaming as
  plain text exactly as today; the only text-layout change is spacing
  around the new progress lines so they read as clearly separate from the
  answer.
- No change to `--json`/`--output-format json` output: `run_once` already
  redirects the `console` used by `_stream_turn` to `/dev/null` in that
  mode (`luna/core/session.py:339`), so the new progress UI is silenced
  there automatically, same as today's tool lines.
- No new dependency — everything here is built on `rich`, already a
  project dependency.

## Components

### `luna/ui/colors.py` (new)

`_hex_to_rgb`, `_rgb_to_hex`, `_lerp_rgb` move here verbatim from
`luna/ui/splash.py` (currently the only place they live). `splash.py`
imports them from here instead of defining them; `_gradient_stops` (only
used by the wordmark) stays in `splash.py`, since the new progress module
has no use for multi-stop gradients — its animation is a 2-color pulse
(see below). This removes the only color-math duplication the new module
would otherwise introduce.

### `luna/ui/progress.py` (new)

Owns the live indicator. Public surface:

```python
class ToolProgress:
    def __init__(self, console: Console) -> None: ...

    def start(self, tool_call_id: str, name: str, args: dict) -> None:
        """Register a newly-requested tool call; begins/updates the live display."""

    def finish(self, tool_call_id: str, ok: bool, detail: str) -> None:
        """Report a call as complete: stop tracking it, print its "done" line."""

    def pause(self) -> None:
        """Stop the live redraw without losing pending state (before an approval prompt)."""

    def resume(self) -> None:
        """Restart the live redraw after `pause()`, if any calls are still pending."""

    def close(self) -> None:
        """Stop the live redraw for good (end of turn); asserts nothing is left pending."""
```

Internals:

- One `rich.live.Live` instance (not `Console.status`, since a plain
  `Status` only supports a single spinner+text line — we need an
  arbitrary number of concurrently pending lines when the model issues
  parallel tool calls). Created lazily on the first `start()` call,
  `transient=True` (pending lines disappear from the live region once
  printed as permanent "done" lines via `console.print`, exactly as
  Rich's own docs describe printing "above" a `Live`), `refresh_per_second=10`.
- `self._pending: dict[str, _Pending]`, `_Pending` is a small dataclass:
  `tool_call_id`, `label` (the precomputed "write_file(luna/x.py)"-style
  string from `_summarize_call`), `started_at: float` (`time.monotonic()`).
- The live renderable is rebuilt on every refresh tick from
  `self._pending.values()`: one `rich.text.Text` line per pending call,
  `"⏺ {label}  ● ● ●  {elapsed:.0f}s"`. The three `●` (U+25CF, the same
  glyph already used for the "● luna" turn-open rule) pulse independently
  in brightness rather than cycling through different shapes — a "breathing"
  look rather than a spinning one: `phase = time.monotonic() * 2.4 + i * 1.4`
  (radians-ish constant, tuned by eye — not claimed exact), brightness
  `0.35 + 0.65 * (0.5 + 0.5 * sin(phase))`, color `_lerp_rgb(PALETTE["blue"],
  PALETTE["peri"], brightness)` — the same two colors already used for
  "quiet secondary text" vs. "readable accent text" elsewhere in the UI,
  so the pulse reads as on-brand rather than a generic loader.
- `finish()` removes the entry from `_pending`, then
  `console.print(Text(f"  ⎿ {detail}", style=PALETTE["blue"]))` — a
  *permanent* line (plain `console.print`, not part of the Live
  renderable), left in the scrollback. If `_pending` is now empty, the
  `Live` is stopped (nothing left to animate); it restarts lazily on the
  next `start()` if the model issues another call before the turn ends.
- `pause()`/`resume()` wrap `Live.stop()`/`Live.start()` — needed because
  an approval prompt uses blocking `input()` on the same console, which
  must not race the Live's background refresh thread for control of the
  terminal.
- Auto-refresh relies on Rich's own background thread inside `Live`
  (started by `Live.start()`), so the dots keep pulsing even while the
  main thread is blocked inside `next()` on `agent.stream()` waiting for
  the next chunk (a long shell command, a slow model response) — no
  polling or manual tick-pumping needed on Luna's side.

### `luna/ui/turn.py` (modified)

`tool_line` (the current single-line "done" printer) is removed — its
start-of-call and end-of-call responsibilities move to `ToolProgress` and
`_summarize_call` in `progress.py`. `open_turn`/`close_turn` are unchanged.

### `luna/core/session.py` (modified)

- `_stream_turn` constructs one `ToolProgress(console)` per turn (mirrors
  the existing per-turn `TurnUsage()`), calls `.close()` in a `finally`
  around the streaming loop so a raised exception mid-turn never leaves a
  stuck spinner.
- New `_report_tool_calls(chunk, progress, requested)` (parallels the
  existing `_report_tools`): scans `chunk.values()` the same
  defensive way `_report_tools` already does (no hardcoded node name —
  robust to whichever node name the installed `deepagents`/`langgraph`
  version uses for the model step, sidestepping the need to hardcode
  `"model"`), looking for an `AIMessage`/`AIMessageChunk` with non-empty
  `.tool_calls`. For each `tool_call` whose `id` is not already in
  `requested` (a per-turn `set[str]`, mirrors the existing `seen` set),
  calls `progress.start(call["id"], call["name"], call["args"])` and adds
  the id to `requested`.
- `_report_tools` (existing) gains a `progress: ToolProgress` parameter:
  after building `body`/`name` as today, it now calls
  `progress.finish(msg.tool_call_id, ok=<no-error heuristic>, detail=<summary>)`
  instead of `tool_line(...)`. `ok`/error heuristic: `ToolMessage.status`
  when the installed LangChain version sets it (`"error"` vs default),
  falling back to "always ok" if the attribute is absent — matches how
  little today's code already assumes about error signaling.
- Both new/changed functions are called from the same two spots
  `_report_tools` is called from today (`_stream_turn`'s per-chunk
  `updates` handling) — `_report_tool_calls` runs first (a call must be
  registered as pending before its result can complete it), then the
  existing tool-result handling.
- Around the existing interrupt-handling block (the `if not interrupts: ...`
  / `collect_decisions(...)` section), add `progress.pause()` right
  before printing the approval prompt and `progress.resume()` right
  after — bounding exactly the section that already does blocking
  console I/O today.

### `_summarize_call(name: str, args: dict) -> str` (new, in `progress.py`)

Pure function, the "informative line" logic:

| tool | summary |
|---|---|
| `write_file`, `edit_file`, `read_file`, `delete` | `f"{name}({args['file_path']})"` |
| `execute` | `f"execute({args['command'][:60]})"` (truncated, matches the existing 120-char truncation precedent for tool result bodies, just tighter since this sits inline in a status line) |
| `glob`, `grep` | `f"{name}({args['pattern']})"` |
| `task` | `f"task({args['subagent_type']})"` — the `description` arg is shown as a second, dimmer line under the pending entry, not squeezed into the same line |
| anything else (`ls`, `manage_mcp`, `manage_skills`, MCP tools, unknown) | `name` alone, unchanged from today |

Every key access is defensive (`args.get(...)`, falling back to bare
`name` on a `KeyError`/missing key) — a future tool schema change must
degrade to today's plain behavior, never crash a turn.

## Rendered example

```
⏺ write_file(luna/ui/progress.py)  ● ● ●  2s
```
resolves to:
```
  ⎿ done · 0.4s
```

A subagent call:
```
⏺ task(researcher)  ● ● ●  8s
    investigates luna/core structure
```
resolves to (the detail is the first line of the subagent's final answer,
the same value `_report_tools` already extracts today):
```
  ⎿ done in 14s · found 3 files, proposed a plan
```

Two parallel calls render as two stacked pending lines, each with its own
independent dot phase and elapsed counter; each resolves to its own `⎿`
line independently, in whatever order their results actually arrive.

## Testing

All new tests use a fake/minimal `agent.stream` (no real model call),
following the existing pattern in `tests/test_session_reload.py`
(`_FakeAgent`). New coverage:

- `tests/test_progress.py` (new): `_summarize_call` for every tool
  category in the table above plus an unknown-tool fallback and a
  malformed-args fallback; `ToolProgress.start`/`finish` bookkeeping
  (pending count, a `finish` for an unregistered id is a no-op rather
  than a crash — defensive, mirrors the `seen` dedup pattern); pulse
  color math is deterministic for a fixed `time.monotonic()` (inject a
  clock function rather than reading the real clock, so the test isn't
  timing-flaky).
- `tests/test_session.py` / `tests/test_repl_flow.py` (extended): a fake
  `agent.stream` that yields an `updates` chunk with an `AIMessage`
  carrying `tool_calls` followed later by the matching `ToolMessage`
  chunk asserts both the pending line's informative label and the
  final `⎿` line appear in the captured output (via a `no_color=True`
  `Console` writing to `io.StringIO`, the same harness every existing
  splash/session test already uses); a parallel-tool-calls case asserts
  both labels appear regardless of completion order; an interrupt case
  asserts `pause`/`resume` are invoked around `collect_decisions` (spy/
  monkeypatch on `ToolProgress`, not a real terminal check — Rich's
  actual Live redraw isn't something a plain string-content assertion
  can verify).

## Open implementation risk to verify early

The `updates`-chunk shape for the model's own node (which node name
carries the `AIMessage` with `tool_calls`) is inferred from the existing
`meta.get("langgraph_node") == "model"` check in the `messages`-mode
branch a few lines above, not verified against a live model call in this
spec's research. The design deliberately does **not** hardcode that node
name for `updates` (see `_report_tool_calls` above, which scans
`chunk.values()` generically instead) specifically to avoid depending on
that assumption. The implementer's first task should include a short
manual smoke check (a real `luna` run, or a fixture recorded from one)
confirming an `AIMessage` with populated `tool_calls` does appear in an
`updates` chunk before its `ToolMessage`, before writing the rest of the
feature against that assumption.
