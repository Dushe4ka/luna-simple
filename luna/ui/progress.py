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
    """Compute a blue->peri brightness pulse, phase-shifted per dot for a "breathing" look."""
    phase = clock_value * _PULSE_SPEED + dot_index * _PULSE_DOT_OFFSET
    brightness = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(phase))
    return _rgb_to_hex(
        _lerp_rgb(_hex_to_rgb(PALETTE["blue"]), _hex_to_rgb(PALETTE["peri"]), brightness)
    )


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
        """Return the number of tool calls currently tracked as in flight."""
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
        out = Text()
        if not self._pending:
            # ``rich.live.Live`` calls ``get_renderable`` eagerly at construction
            # time and again (unconditionally) from ``stop()``. By the time the
            # ``stop()`` call fires, ``finish()`` has already popped the entry
            # being closed out, so there is nothing left to render — return
            # without touching the clock so a call that starts and finishes
            # within a single tick doesn't burn an extra, unaccounted-for
            # ``clock()`` invocation.
            return out
        now = self._clock()
        # Snapshot into a list before iterating: `rich.live.Live`'s background
        # refresh thread calls this ~10x/second while `start()`/`finish()`/
        # `close()` mutate `self._pending` from the main thread with no lock.
        # `list(dict.values())` is atomic under the GIL, so this closes the
        # race (a raw `.values()` iterator can raise "dictionary changed size
        # during iteration" if a mutation lands mid-refresh, silently killing
        # the background thread and freezing the pulse for the rest of the turn).
        for i, pending in enumerate(list(self._pending.values())):
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
