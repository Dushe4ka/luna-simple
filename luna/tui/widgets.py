"""Shared small widgets used across the TUI's panes.

``PulseGlyph`` is the TUI's port of ``luna/ui/progress.py``'s breathing-dot
indicator: a twinkling glyph whose color breathes between the theme's
``primary`` and ``accent`` (``luna.ui.motion.breath``), so the REPL and the
TUI feel like one product. ANSI themes cannot blend colors and keep a
static ``primary``.
"""

from __future__ import annotations

import time

from textual.content import Content
from textual.events import Mount
from textual.widgets import Static

from luna.ui.motion import breath

#: A slow twinkle: mostly the four-pointed star, with brief thinner phases —
#: "мерцание звёздочки" (a blinking asterisk), not a spinning wheel.
_GLYPHS = ("✳", "✦", "✳", "✧")
#: Seconds per glyph frame; also the period of the app's one shared pulse timer.
GLYPH_SECONDS = 0.4
#: Seconds per full breath (dim -> bright -> dim) through the color pulse.
_PULSE_SECONDS = 3.2


def _is_hex(value: str) -> bool:
    return value.startswith("#") and len(value) == 7


class PulseGlyph(Static):
    """A single-line label whose leading glyph twinkles and breathes color.

    Used both as the "Luna is thinking" placeholder in the chat transcript
    (before the first token of a reply arrives) and as each running tool
    call's row (``ToolRow``) in that same transcript.

    ``render()`` computes the frame from elapsed wall-clock time; the
    widget has no timer of its own (no ``auto_refresh``): ``LunaApp`` runs
    one shared ``set_interval`` that refreshes every glyph, instead of one
    timer per running tool row. Like Textual's ``LoadingIndicator`` it
    honours ``app.animation_level``: with ``none`` it is a static label.
    """

    # Styling (including the static fallback $primary color) lives in
    # luna/tui/luna.tcss, not a DEFAULT_CSS string here.

    def __init__(
        self, label: str, *, started_at: float | None = None, id: str | None = None
    ) -> None:
        super().__init__(id=id)
        self.label_text = label
        #: When set, an elapsed-seconds counter is appended. Not used by
        #: the chat's "thinking" placeholder, which has no meaningful single
        #: start time (it spans tool calls too).
        self._started_at = started_at
        self._mounted_at = 0.0

    def _on_mount(self, _: Mount) -> None:
        self._mounted_at = time.monotonic()

    def _color(self, elapsed: float) -> str:
        """Breathing ``primary -> accent`` color; static ``primary`` if not blendable."""
        variables = self.app.theme_variables
        primary = variables.get("primary", "")
        accent = variables.get("accent", primary)
        if not (_is_hex(primary) and _is_hex(accent)):
            return primary
        return breath(elapsed, _PULSE_SECONDS, primary, accent)

    def render(self) -> Content:
        """Render the current frame: a static bullet, or the twinkling glyph."""
        label = self.label_text
        if self._started_at is not None:
            label = f"{label} · {time.monotonic() - self._started_at:.0f}s"
        if self.app.animation_level == "none":
            # A static bullet, no cycling glyph or live-updating timer —
            # matches how Textual's own LoadingIndicator falls back to
            # plain "Loading..." text under this same setting.
            return Content(f"⏺ {label}")
        elapsed = time.monotonic() - self._mounted_at
        glyph = _GLYPHS[int(elapsed / GLYPH_SECONDS) % len(_GLYPHS)]
        return Content.styled(f"{glyph} {label}", self._color(elapsed))
