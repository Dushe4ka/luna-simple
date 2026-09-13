# Luna Tool-Progress UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While an agent turn is running, show a live pulsing-dot indicator
for every in-flight tool call (including subagent/`task` delegations,
currently completely silent while they run), with an informative,
type-specific one-line label, replacing today's "nothing until it's done"
silence and generic post-hoc tool line.

**Architecture:** A new `ToolProgress` class (`luna/ui/progress.py`) owns
one `rich.live.Live` region per turn, populated from a dict of pending
tool calls. `luna/core/session.py`'s `_stream_turn` detects a tool call's
*start* (an `AIMessage.tool_calls` appearing in an `updates`-mode stream
chunk — confirmed empirically below, not merely inferred) as well as its
existing detection of a call's *completion* (the matching `ToolMessage`),
feeding both into `ToolProgress`. The indicator's animation runs on Rich's
own background refresh thread, so it keeps pulsing even while the main
thread is blocked waiting on a slow tool or an opaque, synchronous
subagent `invoke()` call.

**Tech Stack:** Python 3.12, `rich` (`Live`, `Text`, `Style` — already a
project dependency, no new one added), `langchain_core.messages`
(`AIMessage`, `ToolMessage` — already used by `session.py`).

**Spec:** `docs/superpowers/specs/2026-09-13-luna-tool-progress-ui-design.md`

## Global Constraints

- No new dependency — everything is built on `rich`, already installed.
- `--json`/`--output-format json` output must stay silent: `run_once`
  already redirects `_stream_turn`'s `console` to `/dev/null` in that
  mode (`luna/core/session.py`, current line 339) — the new progress UI
  must not bypass that redirection (i.e. it must only ever print through
  the `console` it's given, never `print()`/`sys.stdout` directly).
- No live/incremental Markdown rendering of the assistant's answer text —
  the answer keeps streaming as plain text exactly as today.
- No live view into a subagent's internal tool calls — `task` is one
  opaque timed call (spinner + elapsed timer + one-line summary on
  completion), never a nested step feed.
- Framework-import confinement: `deepagents`/`langgraph`/
  `langchain_mcp_adapters` imports stay confined to
  `luna/core/{agent,session,persistence,toolguard}.py` (plus two
  pre-existing, unrelated exceptions documented in `AGENTS.md`). This plan
  touches `luna/core/session.py` (already on that list) and adds no new
  framework import anywhere else — `luna/ui/progress.py` only imports
  `rich` and `luna.ui.{colors,theme}`.

## Verified fact this plan depends on

The spec flagged an open risk: which `langgraph` node's `updates` chunk
actually carries the `AIMessage` with populated `tool_calls`, and whether
it arrives before the matching `ToolMessage`. This was verified directly
against this repo's real `build_agent()` + the existing `FakeToolCallingModel`
test fixture (no guessing, no reliance on `langgraph`/`deepagents` docs):

```
UPDATES keys: ['model']
  node= model AIMessage tool_calls= [{'name': 'write_file', 'args': {'file_path': '/a.py', 'content': 'x=1'}, 'id': '1', 'type': 'tool_call'}] content= ''
MESSAGES node= tools ToolMessage 'Updated file /a.py'
UPDATES keys: ['tools']
  node= tools ToolMessage tool_calls= None content= 'Updated file /a.py'
```

Confirmed: node `"model"`'s `updates` chunk carries the `AIMessage` with
`tool_calls` fully populated (name, args, id), strictly before node
`"tools"`'s `updates` chunk carries the matching `ToolMessage`. Task 3
below still scans `chunk.values()` generically (not hardcoding `"model"`)
to match the existing defensive style of `_report_tools`, but the
ordering and shape are now a verified fact, not an assumption.

---

### Task 1: Extract shared color math into `luna/ui/colors.py`

**Files:**
- Create: `luna/ui/colors.py`
- Modify: `luna/ui/splash.py:31,43-60` (remove the moved definitions, import them instead)
- Create: `tests/test_colors.py`
- Modify: `tests/test_splash.py:1-30` (remove the three tests that move to `test_colors.py`, trim the import list)

**Interfaces:**
- Produces: `luna.ui.colors.RGB` (`tuple[int, int, int]`), `luna.ui.colors._hex_to_rgb(hexcolor: str) -> RGB`, `luna.ui.colors._rgb_to_hex(rgb: RGB) -> str`, `luna.ui.colors._lerp_rgb(a: RGB, b: RGB, t: float) -> RGB` — Task 2 (`luna/ui/progress.py`) imports all four of these.

- [ ] **Step 1: Create `luna/ui/colors.py`**

```python
"""Shared RGB color math for Luna's truecolor UI (splash wordmark, tool
progress pulse) — pure functions, no rendering, no rich dependency."""

from __future__ import annotations

RGB = tuple[int, int, int]


def _hex_to_rgb(hexcolor: str) -> RGB:
    """``"#aabbcc" -> (0xaa, 0xbb, 0xcc)``."""
    h = hexcolor.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgb_to_hex(rgb: RGB) -> str:
    """``(0xaa, 0xbb, 0xcc) -> "#aabbcc"``."""
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _lerp_rgb(a: RGB, b: RGB, t: float) -> RGB:
    """Linear-interpolate between two RGB triples; ``t=0`` is ``a``, ``t=1`` is ``b``."""
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]
```

- [ ] **Step 2: Create `tests/test_colors.py`**

```python
from luna.ui.colors import _hex_to_rgb, _lerp_rgb, _rgb_to_hex


def test_hex_to_rgb_and_back_roundtrip():
    assert _hex_to_rgb("#8a9cff") == (0x8A, 0x9C, 0xFF)
    assert _rgb_to_hex((0x8A, 0x9C, 0xFF)) == "#8a9cff"
    for hexcolor in ("#000000", "#ffffff", "#5566a8"):
        assert _rgb_to_hex(_hex_to_rgb(hexcolor)) == hexcolor


def test_lerp_rgb_endpoints_return_exact_colors():
    a, b = (10, 20, 30), (200, 100, 0)
    assert _lerp_rgb(a, b, 0.0) == a
    assert _lerp_rgb(a, b, 1.0) == b


def test_lerp_rgb_midpoint_is_the_average():
    a, b = (0, 0, 0), (100, 200, 50)
    assert _lerp_rgb(a, b, 0.5) == (50, 100, 25)
```

- [ ] **Step 3: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_colors.py -v`
Expected: 3 passed (this module has no dependency on `splash.py` yet, so
these already pass against the freshly created file).

- [ ] **Step 4: Update `luna/ui/splash.py`** — remove the moved code, import from `colors.py`

Replace lines 31 and 43-60 (the `RGB = ...` alias and the entire
`# --- Colour math ---` section down to the end of `_lerp_rgb`) so the
top of the file reads:

```python
from __future__ import annotations

import time

from rich.console import Console
from rich.style import Style
from rich.text import Text

from luna import __version__
from luna.ui.colors import RGB, _hex_to_rgb, _lerp_rgb, _rgb_to_hex
from luna.ui.theme import PALETTE

_DEFAULT_STEPS = [
    "loading modules ...",
    "connecting to tools ...",
    "preparing your canvas ...",
    "almost there ...",
]

_TAGLINE = "YOUR AI AGENT COMPANION"
_SUBTITLE = "LUNA - a quiet intelligence for navigating complex systems"
_MAX_WIDTH = 118

# Big "L U N A" wordmark (5 rows), painted with a left-to-right gradient.
_WORDMARK = [
    "█        █    █    █    █     ███  ",
    "█        █    █    █    ██   █   █ ",
    "█        █    █    █ █  █    ██████",
    "█        █    █    █  █ █    █    █",
    "██████    ████     █   ██    █    █",
]
_WORDMARK_GRADIENT = [PALETTE["peri"], PALETTE["moon"], PALETTE["mauve"], PALETTE["accent"]]


def _gradient_stops(colors: list[str], steps: int) -> list[str]:
    """Sample a smooth multi-stop hex gradient at ``steps`` evenly-spaced points.

    ``steps <= 1`` returns just the first color. The endpoints of ``colors``
    are always hit exactly (no interpolation drift at the boundaries).
    """
    if steps <= 1:
        return [colors[0]]
    rgb_stops = [_hex_to_rgb(c) for c in colors]
    segments = len(rgb_stops) - 1
    out: list[str] = []
    for i in range(steps):
        t = i / (steps - 1) * segments
        seg = min(int(t), segments - 1)
        local_t = t - seg
        out.append(_rgb_to_hex(_lerp_rgb(rgb_stops[seg], rgb_stops[seg + 1], local_t)))
    return out
```

Everything from `# --- The wordmark ---` onward (`_wordmark_lines` through
the end of the file) is unchanged.

- [ ] **Step 5: Trim `tests/test_splash.py`**

Remove `test_hex_to_rgb_and_back_roundtrip`, `test_lerp_rgb_endpoints_return_exact_colors`,
and `test_lerp_rgb_midpoint_is_the_average` (now covered by `test_colors.py`), and
change the import block at the top to:

```python
from luna.ui.splash import _gradient_stops, render_splash
```

The remaining tests (`test_gradient_stops_*`, `test_renders_at_various_widths`,
`test_full_splash_has_wordmark_version_and_tagline`,
`test_compact_fallback_is_used_when_narrow`, `test_wordmark_emits_truecolor_ansi_codes`)
are unchanged.

- [ ] **Step 6: Run the full test file set to verify nothing broke**

Run: `uv run pytest tests/test_splash.py tests/test_colors.py -v`
Expected: all pass (12 in `test_splash.py` minus the 3 moved = 9, plus 3 in `test_colors.py`).

- [ ] **Step 7: Lint**

Run: `uv run ruff check luna/ui/colors.py luna/ui/splash.py tests/test_colors.py tests/test_splash.py && uv run ruff format --check luna/ui/colors.py luna/ui/splash.py tests/test_colors.py tests/test_splash.py`
Expected: clean. If format check fails, run `uv run ruff format` on those paths and re-check.

- [ ] **Step 8: Commit**

```bash
git add luna/ui/colors.py luna/ui/splash.py tests/test_colors.py tests/test_splash.py
git commit -m "refactor: extract RGB color math from splash.py into luna/ui/colors.py"
```

---

### Task 2: `luna/ui/progress.py` — the live tool-progress indicator

**Files:**
- Create: `luna/ui/progress.py`
- Test: `tests/test_progress.py`

**Interfaces:**
- Consumes: `luna.ui.colors.{RGB,_hex_to_rgb,_rgb_to_hex,_lerp_rgb}` (Task 1), `luna.ui.theme.PALETTE`.
- Produces: `ToolProgress` with methods `start(tool_call_id: str, name: str, args: dict) -> None`,
  `finish(tool_call_id: str, ok: bool, detail: str) -> None`, `pause() -> None`,
  `resume() -> None`, `close() -> None`, plus the module-level pure function
  `_summarize_call(name: str, args: dict) -> str` — Task 3 (`luna/core/session.py`)
  constructs `ToolProgress(console)` and calls `start`/`finish`/`pause`/`resume`/`close` on it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_progress.py`:

```python
import io

from rich.console import Console

from luna.ui.progress import ToolProgress, _pulse_color, _summarize_call
from luna.ui.theme import PALETTE


def _console():
    return Console(file=io.StringIO(), force_terminal=True, no_color=True)


def test_summarize_call_for_file_tools_shows_the_path():
    for name in ("write_file", "edit_file", "read_file", "delete"):
        assert _summarize_call(name, {"file_path": "/a/b.py"}) == f"{name}(/a/b.py)"


def test_summarize_call_for_execute_shows_and_truncates_the_command():
    assert _summarize_call("execute", {"command": "ls -la"}) == "execute(ls -la)"
    long_cmd = "x" * 80
    result = _summarize_call("execute", {"command": long_cmd})
    assert result == f"execute({'x' * 60}…)"


def test_summarize_call_for_glob_and_grep_shows_the_pattern():
    assert _summarize_call("glob", {"pattern": "**/*.py"}) == "glob(**/*.py)"
    assert _summarize_call("grep", {"pattern": "TODO"}) == "grep(TODO)"


def test_summarize_call_for_task_shows_the_subagent_type():
    assert _summarize_call("task", {"subagent_type": "researcher", "description": "look around"}) == "task(researcher)"


def test_summarize_call_falls_back_to_bare_name_on_missing_args():
    assert _summarize_call("write_file", {}) == "write_file"
    assert _summarize_call("execute", {}) == "execute"
    assert _summarize_call("task", {}) == "task"


def test_summarize_call_for_unknown_tool_is_just_the_name():
    assert _summarize_call("ls", {"path": "/x"}) == "ls"
    assert _summarize_call("manage_mcp", {"action": "list"}) == "manage_mcp"


def test_pulse_color_is_deterministic_for_a_fixed_clock_value():
    a = _pulse_color(1.0, 0)
    b = _pulse_color(1.0, 0)
    assert a == b
    # different dots at the same instant are (almost always) out of phase
    assert _pulse_color(1.0, 0) != _pulse_color(1.0, 1)


def test_tool_progress_tracks_pending_count():
    progress = ToolProgress(_console(), clock=lambda: 0.0)
    assert progress.pending_count() == 0
    progress.start("1", "write_file", {"file_path": "/a.py"})
    assert progress.pending_count() == 1
    progress.start("2", "execute", {"command": "ls"})
    assert progress.pending_count() == 2
    progress.finish("1", True, "done")
    assert progress.pending_count() == 1
    progress.finish("2", True, "")
    assert progress.pending_count() == 0
    progress.close()


def test_tool_progress_finish_prints_a_permanent_detail_line_including_the_label():
    """The label (tool + its key arg) must appear in the PERMANENT done line,
    not only in the transient pending animation. Rich's ``Live.start()``
    defaults to ``refresh=False`` — its first frame only renders on the
    background thread's first tick (~100ms at the default refresh rate) or
    on ``stop()``, which by then has already popped the finished entry out
    of the pending dict. A call that starts and finishes faster than one
    tick (true of every call in this synchronous test suite, and of many
    real fast calls like `ls`) would otherwise show its label nowhere at
    all — this test guards against exactly that regression."""
    console = _console()
    progress = ToolProgress(console, clock=lambda: 0.0)
    progress.start("1", "write_file", {"file_path": "/a.py"})
    progress.finish("1", True, "Updated file /a.py")
    progress.close()
    out = console.file.getvalue()
    assert "write_file(/a.py) · done" in out
    assert "Updated file /a.py" in out


def test_tool_progress_finish_for_task_uses_in_wording_and_integer_seconds():
    ticks = iter([0.0, 0.0, 14.3])
    console = _console()
    progress = ToolProgress(console, clock=lambda: next(ticks))
    progress.start("1", "task", {"subagent_type": "researcher", "description": "look around"})
    progress.finish("1", True, "found 3 files")
    progress.close()
    out = console.file.getvalue()
    assert "task(researcher) · done in 14s · found 3 files" in out


def test_tool_progress_finish_for_a_failed_call_says_error():
    console = _console()
    progress = ToolProgress(console, clock=lambda: 0.0)
    progress.start("1", "execute", {"command": "false"})
    progress.finish("1", False, "exit code 1")
    progress.close()
    out = console.file.getvalue()
    assert "execute(false) · error" in out
    assert "exit code 1" in out


def test_tool_progress_finish_on_unknown_id_is_a_no_op():
    console = _console()
    progress = ToolProgress(console, clock=lambda: 0.0)
    progress.finish("never-started", True, "x")  # must not raise
    progress.close()
    assert console.file.getvalue() == ""


def test_tool_progress_pause_and_resume_do_not_lose_pending_state():
    progress = ToolProgress(_console(), clock=lambda: 0.0)
    progress.start("1", "write_file", {"file_path": "/a.py"})
    progress.pause()
    assert progress.pending_count() == 1
    progress.resume()
    assert progress.pending_count() == 1
    progress.finish("1", True, "")
    progress.close()


def test_render_pending_shows_the_label_and_a_second_line_for_task_description():
    progress = ToolProgress(_console(), clock=lambda: 0.0)
    progress.start("1", "task", {"subagent_type": "researcher", "description": "look around"})
    rendered = progress._render_pending().plain
    assert "task(researcher)" in rendered
    assert "look around" in rendered
    progress.finish("1", True, "")
    progress.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_progress.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna.ui.progress'`

- [ ] **Step 3: Create `luna/ui/progress.py`**

```python
"""A live, animated indicator for in-flight agent tool calls.

While a tool call (including an opaque, blocking subagent ``task``
delegation) is running, this shows a pulsing-dot line with an elapsed
timer; on completion it prints a permanent one-line summary. Built
entirely on ``rich.live.Live`` — its own background refresh thread keeps
the pulse animating even while the caller is blocked waiting on the next
streamed chunk (a slow shell command, an opaque subagent invocation).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console
from rich.live import Live
from rich.style import Style
from rich.text import Text

from luna.ui.colors import _hex_to_rgb, _lerp_rgb, _rgb_to_hex
from luna.ui.theme import PALETTE

_DOT = "●"
_PULSE_SPEED = 2.4
_PULSE_DOT_OFFSET = 1.4
_DESCRIPTION_MAX = 80


def _summarize_call(name: str, args: dict) -> str:
    """One-line, type-specific label for a tool call — the pending/done line's headline."""
    if name in ("write_file", "edit_file", "read_file", "delete"):
        path = args.get("file_path")
        return f"{name}({path})" if path else name
    if name == "execute":
        command = args.get("command")
        if not command:
            return name
        if len(command) > 60:
            command = command[:60] + "…"
        return f"execute({command})"
    if name in ("glob", "grep"):
        pattern = args.get("pattern")
        return f"{name}({pattern})" if pattern else name
    if name == "task":
        subagent_type = args.get("subagent_type")
        return f"task({subagent_type})" if subagent_type else name
    return name


def _pulse_color(clock_value: float, dot_index: int) -> str:
    """A blue->peri brightness pulse, phase-shifted per dot for a "breathing" look."""
    phase = clock_value * _PULSE_SPEED + dot_index * _PULSE_DOT_OFFSET
    brightness = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(phase))
    return _rgb_to_hex(_lerp_rgb(_hex_to_rgb(PALETTE["blue"]), _hex_to_rgb(PALETTE["peri"]), brightness))


@dataclass
class _Pending:
    name: str
    label: str
    detail: str | None
    started_at: float


class ToolProgress:
    """Tracks in-flight tool calls for one turn and renders a live pulse indicator."""

    def __init__(self, console: Console, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._console = console
        self._clock = clock
        self._pending: dict[str, _Pending] = {}
        self._live: Live | None = None

    def pending_count(self) -> int:
        return len(self._pending)

    def start(self, tool_call_id: str, name: str, args: dict) -> None:
        """Register a newly-requested tool call and ensure the live display is running."""
        if tool_call_id in self._pending:
            return
        detail = args.get("description") if name == "task" else None
        if detail and len(detail) > _DESCRIPTION_MAX:
            detail = detail[:_DESCRIPTION_MAX] + "…"
        self._pending[tool_call_id] = _Pending(
            name=name,
            label=_summarize_call(name, args),
            detail=detail,
            started_at=self._clock(),
        )
        self._ensure_live()

    def finish(self, tool_call_id: str, ok: bool, detail: str) -> None:
        """Report a call as complete: stop tracking it, print its permanent detail line.

        The label (``pending.label``, e.g. ``"write_file(/a.py)"``) is repeated
        here rather than relying on the transient pending line to have shown
        it: ``Live.start()`` defaults to ``refresh=False``, so a call that
        finishes before the background thread's first tick (~100ms at the
        default refresh rate — true of any fast call) would otherwise never
        have its label appear anywhere in the output.
        """
        pending = self._pending.pop(tool_call_id, None)
        if pending is None:
            return
        elapsed = self._clock() - pending.started_at
        status = "done" if ok else "error"
        if pending.name == "task":
            line = f"  ⎿ {pending.label} · {status} in {elapsed:.0f}s"
        else:
            line = f"  ⎿ {pending.label} · {status} · {elapsed:.1f}s"
        if detail:
            line += f" · {detail}"
        style = PALETTE["blue"] if ok else PALETTE["mauve"]
        self._console.print(Text(line, style=style))
        if not self._pending:
            self._stop_live()

    def pause(self) -> None:
        """Stop the live redraw without losing pending state (before a blocking prompt)."""
        self._stop_live()

    def resume(self) -> None:
        """Restart the live redraw after :meth:`pause`, if any calls are still pending."""
        if self._pending:
            self._ensure_live()

    def close(self) -> None:
        """Stop the live redraw for good (end of turn, or an error mid-turn)."""
        self._pending.clear()
        self._stop_live()

    def _render_pending(self) -> Text:
        now = self._clock()
        out = Text()
        for i, pending in enumerate(self._pending.values()):
            if i:
                out.append("\n")
            elapsed = now - pending.started_at
            out.append("⏺ ")
            out.append(pending.label, style=PALETTE["accent"])
            out.append("  ")
            for d in range(3):
                out.append(_DOT, style=Style(color=_pulse_color(now, d)))
                out.append(" ")
            out.append(f" {elapsed:.0f}s", style=PALETTE["blue"])
            if pending.detail:
                out.append("\n    ")
                out.append(pending.detail, style=PALETTE["blue"])
        return out

    def _ensure_live(self) -> None:
        if self._live is None:
            self._live = Live(
                get_renderable=self._render_pending,
                console=self._console,
                refresh_per_second=10,
                transient=True,
            )
            self._live.start()

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_progress.py -v`
Expected: all pass.

- [ ] **Step 5: Lint**

Run: `uv run ruff check luna/ui/progress.py tests/test_progress.py && uv run ruff format --check luna/ui/progress.py tests/test_progress.py`
Expected: clean (fix with `uv run ruff format` if needed, then re-check).

- [ ] **Step 6: Commit**

```bash
git add luna/ui/progress.py tests/test_progress.py
git commit -m "feat: ToolProgress — live pulsing-dot indicator for in-flight tool calls"
```

---

### Task 3: Wire `ToolProgress` into the REPL turn loop

**Files:**
- Modify: `luna/core/session.py:1-49` (imports), `:157-235` (`_report_tools`, `_stream_turn`)
- Modify: `luna/ui/turn.py:21-27` (remove `tool_line`, now unused)
- Modify: `tests/test_repl_flow.py` (extend)
- Modify: `tests/test_session.py` (extend)

**Interfaces:**
- Consumes: `luna.ui.progress.ToolProgress` (Task 2): `start(tool_call_id, name, args)`,
  `finish(tool_call_id, ok, detail)`, `pause()`, `resume()`, `close()`.
- Produces: no new public interface — `_stream_turn`'s existing signature and
  return type (`tuple[str, bool, TurnUsage, set[str]]`) are unchanged, so
  `run_once`/`run_repl` (its only callers) need no changes.

- [ ] **Step 1: Remove `tool_line` from `luna/ui/turn.py`**

Delete this function (lines 21-27 of the current file) — its job moves
into `ToolProgress.finish`:

```python
def tool_line(console: Console, name: str, summary: str = "") -> None:
    """One dim, indented line describing a tool call/result."""
    text = Text("  ⚙ ", style=PALETTE["blue"])
    text.append(name, style=f"bold {PALETTE['accent']}")
    if summary:
        text.append(f" · {summary}", style=PALETTE["blue"])
    console.print(text)
```

The rest of `luna/ui/turn.py` (`open_turn`, `close_turn`) is unchanged.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_repl_flow.py` (append at the end of the file):

```python
def test_tool_progress_prints_an_informative_label_and_done_line(tmp_path, fake_model):
    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": "1",
                    "args": {"file_path": "/notes.txt", "content": "hi\n"},
                }
            ],
        ),
        AIMessage(content="wrote notes.txt"),
    ]
    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls)
    )
    console = Console(file=io.StringIO(), force_terminal=True, no_color=True)
    lines = iter(["write notes.txt please", "/exit"])
    rc = run_repl(
        agent,
        console=console,
        input_fn=lambda _: next(lines),
        rebuild=lambda: agent,
        workdir=str(tmp_path),
        config=LunaConfig(workdir=str(tmp_path), yolo=True),
        thread_id="t",
    )
    assert rc == 0
    out = console.file.getvalue()
    assert "write_file(/notes.txt) · done" in out
    assert "Updated file /notes.txt" in out


def test_tool_progress_handles_two_parallel_tool_calls(tmp_path, fake_model):
    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "write_file", "id": "1", "args": {"file_path": "/a.txt", "content": "a\n"}},
                {"name": "write_file", "id": "2", "args": {"file_path": "/b.txt", "content": "b\n"}},
            ],
        ),
        AIMessage(content="wrote both files"),
    ]
    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls)
    )
    console = Console(file=io.StringIO(), force_terminal=True, no_color=True)
    lines = iter(["write a.txt and b.txt", "/exit"])
    rc = run_repl(
        agent,
        console=console,
        input_fn=lambda _: next(lines),
        rebuild=lambda: agent,
        workdir=str(tmp_path),
        config=LunaConfig(workdir=str(tmp_path), yolo=True),
        thread_id="t",
    )
    assert rc == 0
    out = console.file.getvalue()
    assert "write_file(/a.txt) · done" in out
    assert "write_file(/b.txt) · done" in out
```

Add to `tests/test_session.py` (append at the end of the file — this one
drives `_stream_turn` directly with a hand-built fake agent, so it can
assert `ToolProgress.pause`/`resume` bracket the approval prompt without
depending on real Rich `Live` timing):

```python
def test_stream_turn_pauses_and_resumes_progress_around_an_interrupt(monkeypatch):
    from types import SimpleNamespace

    from luna.core import session

    events: list[str] = []

    class _SpyProgress:
        def __init__(self, console):
            events.append("created")

        def start(self, *a, **k):
            events.append("start")

        def finish(self, *a, **k):
            events.append("finish")

        def pause(self):
            events.append("pause")

        def resume(self):
            events.append("resume")

        def close(self):
            events.append("close")

    monkeypatch.setattr(session, "ToolProgress", _SpyProgress)

    tool_call = AIMessage(
        content="", tool_calls=[{"name": "write_file", "id": "1", "args": {"file_path": "/a"}}]
    )
    interrupt = SimpleNamespace(
        value={"action_requests": [{"name": "write_file", "args": {"file_path": "/a"}}]}
    )

    class _FakeAgent:
        def __init__(self):
            self._resumed = False

        def stream(self, payload, config, stream_mode):
            if not self._resumed:
                yield "updates", {"model": {"messages": [tool_call]}}
            else:
                yield "messages", (AIMessage(content="done"), {"langgraph_node": "model"})

        def get_state(self, config):
            if not self._resumed:
                self._resumed = True
                return SimpleNamespace(values={}, interrupts=[interrupt])
            return SimpleNamespace(values={}, interrupts=[])

    console = Console(file=io.StringIO(), force_terminal=True, no_color=True)
    session._stream_turn(
        _FakeAgent(),
        {"messages": [{"role": "user", "content": "hi"}]},
        {"configurable": {"thread_id": "t"}},
        console,
        input_fn=lambda _: "",
    )
    assert events == ["created", "start", "pause", "resume", "close"]
```

This test needs `AIMessage` and `Console`/`io` already imported at the top
of `tests/test_session.py` — add `import io` and
`from langchain_core.messages import AIMessage` if not already present
(the file currently imports `AIMessage` already; check before adding a
duplicate).

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_repl_flow.py tests/test_session.py -v -k "progress or pauses_and_resumes"`
Expected: FAIL (`ImportError`/`AttributeError` — `ToolProgress` not wired
into `session.py` yet, `tool_line`-based output doesn't match the new
assertions).

- [ ] **Step 4: Update `luna/core/session.py` imports**

Change:

```python
from luna.ui.approve import prompt_decision
from luna.ui.theme import PALETTE
from luna.ui.turn import close_turn, open_turn, tool_line
```

to:

```python
from luna.ui.approve import prompt_decision
from luna.ui.progress import ToolProgress
from luna.ui.theme import PALETTE
from luna.ui.turn import close_turn, open_turn
```

- [ ] **Step 5: Replace `_report_tools` and add `_report_tool_calls`**

Replace the current `_report_tools` function (lines 157-175) with:

```python
def _report_tool_calls(chunk: dict, progress: ToolProgress, requested: set[str]) -> None:
    """Register newly-requested tool calls with the live progress indicator.

    Scans generically (no hardcoded node name), matching ``_report_tools``'s
    existing style — verified empirically that the model's own node update
    carries the ``AIMessage`` with populated ``tool_calls`` before the
    matching ``ToolMessage`` shows up in a later chunk, but this does not
    hardcode that node's name.
    """
    for update in chunk.values():
        if not isinstance(update, dict):
            continue
        for msg in update.get("messages", []) or []:
            if not isinstance(msg, AIMessage):
                continue
            for call in msg.tool_calls or []:
                call_id = call.get("id")
                if not call_id or call_id in requested:
                    continue
                requested.add(call_id)
                progress.start(call_id, call["name"], call.get("args") or {})


def _report_tools(chunk: dict, progress: ToolProgress, seen: set[str], names: set[str]) -> bool:
    """Report finished tool calls to the progress indicator; return True if a tool asked for /reload.

    Every reported tool's name is added to ``names``.
    """
    reload_requested = False
    for update in chunk.values():
        if not isinstance(update, dict):
            continue
        for msg in update.get("messages", []) or []:
            if isinstance(msg, ToolMessage) and msg.tool_call_id not in seen:
                seen.add(msg.tool_call_id)
                if msg.name:
                    names.add(msg.name)
                body = str(msg.content) if msg.content else ""
                detail = body.splitlines()[0][:120] if body else ""
                ok = getattr(msg, "status", "success") != "error"
                progress.finish(msg.tool_call_id, ok, detail)
                if msg.name in ("manage_mcp", "manage_skills") and _RELOAD_MARKER in body:
                    reload_requested = True
    return reload_requested
```

- [ ] **Step 6: Update `_stream_turn`**

Replace the current `_stream_turn` (lines 178-235) with:

```python
def _stream_turn(
    agent,
    payload,
    config: dict,
    console: Console,
    input_fn: Callable[[str], str],
    *,
    rules=None,
    workdir: str = ".",
) -> tuple[str, bool, TurnUsage, set[str]]:
    """Run one user turn.

    Returns ``(final_text, reload_requested, turn_usage, tool_names_seen)``.
    """
    parts: list[str] = []
    seen_tools: set[str] = set()
    requested_tools: set[str] = set()
    tool_names_seen: set[str] = set()
    reload_requested = False
    turn_usage = TurnUsage()
    progress = ToolProgress(console)

    open_turn(console)
    try:
        while True:
            interrupts: list = []
            for mode, chunk in agent.stream(
                payload, config=config, stream_mode=["messages", "updates"]
            ):
                if mode == "messages":
                    msg, meta = chunk
                    if meta.get("langgraph_node") == "model" and isinstance(
                        msg, (AIMessage, AIMessageChunk)
                    ):
                        turn_usage.merge(getattr(msg, "usage_metadata", None))
                        text = msg.content if isinstance(msg.content, str) else ""
                        if text:
                            parts.append(text)
                            console.print(text, end="", soft_wrap=True)
                elif mode == "updates":
                    interrupts.extend(_iter_interrupts(chunk))
                    _report_tool_calls(chunk, progress, requested_tools)
                    reload_requested |= _report_tools(chunk, progress, seen_tools, tool_names_seen)

            if not interrupts:
                state = agent.get_state(config)
                interrupts = list(getattr(state, "interrupts", ()) or [])
            if not interrupts:
                break

            progress.pause()
            console.print()
            resume = collect_decisions(
                console,
                interrupts[0].value,
                input_fn=input_fn,
                rules=rules,
                workdir=workdir,
            )
            progress.resume()
            payload = Command(resume=resume)
    finally:
        progress.close()

    close_turn(console)
    return "".join(parts).strip(), reload_requested, turn_usage, tool_names_seen
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_repl_flow.py tests/test_session.py -v`
Expected: all pass, including every pre-existing test in both files (the
`_stream_turn` signature and return value are unchanged, so no other test
should need edits).

- [ ] **Step 8: Run the full test suite**

Run: `uv run pytest -q`
Expected: all pass (this is the point where a stray import of `tool_line`
elsewhere, or a knock-on effect on `luna/ui/turn.py`'s other tests, would
surface).

- [ ] **Step 9: Lint**

Run: `uv run ruff check luna/core/session.py luna/ui/turn.py tests/test_repl_flow.py tests/test_session.py && uv run ruff format --check luna/core/session.py luna/ui/turn.py tests/test_repl_flow.py tests/test_session.py`
Expected: clean (fix with `uv run ruff format` if needed, then re-check).

- [ ] **Step 10: Commit**

```bash
git add luna/core/session.py luna/ui/turn.py tests/test_repl_flow.py tests/test_session.py
git commit -m "feat: wire ToolProgress into the turn loop — live tool-call visibility"
```

---

### Task 4: Documentation

**Files:**
- Modify: `CHANGELOG.md` (add an `[Unreleased]` entry)
- Modify: `AGENTS.md` (itemize `luna/ui/` like every other subpackage)

**Interfaces:**
- Consumes: nothing (prose only).
- Produces: nothing (terminal task).

- [ ] **Step 1: Add a CHANGELOG entry**

In `CHANGELOG.md`, under the existing `## [Unreleased]` / `### Изменено`
section, add a new bullet (after the existing splash-redesign bullet):

```markdown
- Во время хода теперь виден живой индикатор выполнения инструментов:
  пока вызов инструмента (включая делегирование субагенту через `task`)
  ещё выполняется, показывается пульсирующая строка с типовой сводкой —
  путь файла для `write_file`/`edit_file`/`read_file`/`delete`, команда
  для `execute`, паттерн для `glob`/`grep`, имя субагента для `task`; по
  завершении печатается постоянная строка вида
  `⎿ write_file(path) · done · 0.4s · …` (или `· error · …` при ошибке).
  Раньше вызов инструмента был виден только
  постфактум, а делегирование субагенту не показывало вообще ничего, пока
  тот не закончит целиком (ограничение самого `deepagents`: субагент
  выполняется синхронным `invoke()`, поэтому его внутренние шаги
  по-прежнему не видны — таймер и итоговая сводка, без трассировки шагов
  внутри). Без новых зависимостей.
```

- [ ] **Step 2: Itemize `luna/ui/` in `AGENTS.md`**

Replace this single line (in the `## Структура` section):

```markdown
- `luna/ui/` — тема `rich`, заставка, консоль, диалог подтверждения, оформление реплик
```

with:

```markdown
- `luna/ui/` — тема `rich` и весь визуальный слой REPL
  - `theme.py` — цветовая палитра и `rich`-тема
  - `colors.py` — чистая RGB-математика (используется `splash.py` и `progress.py`)
  - `splash.py` — стартовая заставка (градиентный wordmark)
  - `progress.py` — живой индикатор выполнения инструментов во время хода
  - `turn.py` — рамка хода (открывающая/закрывающая линия)
  - `console.py` — консоль `rich`
  - `approve.py` — диалог подтверждения мутирующих действий
```

- [ ] **Step 3: Run the full test suite one more time (docs-only change, but confirms a clean tree)**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all clean.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md AGENTS.md
git commit -m "docs: document the live tool-progress indicator"
```
