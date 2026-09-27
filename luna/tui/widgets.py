"""Shared small widgets used across the TUI's panes.

``PulseGlyph`` is the TUI's port of ``luna/ui/progress.py``'s breathing-dot
indicator — the REPL has had a live "something is happening" animation
since that module was written, but the full-screen TUI never got its own
version: a pending tool call just sat in the activity sidebar as a static
``⏺ name`` line, and a turn with no text yet gave no sign of life at all
between hitting Enter and the first streamed token. This widget is the fix
for both spots, using the same breathing peri->mauve color math as
``progress.py`` (there: blue->peri; here: peri->mauve, so it reads as
"Luna" rather than reusing the REPL's own hue) so the two surfaces feel
like one product.
"""

from __future__ import annotations

import math
import time

from rich.text import Text
from textual.events import Mount
from textual.widgets import Static

from luna.ui.colors import _hex_to_rgb, _lerp_rgb, _rgb_to_hex
from luna.ui.theme import PALETTE

#: A slow twinkle: mostly the four-pointed star, with brief thinner phases —
#: "мерцание звёздочки" (a blinking asterisk), not a spinning wheel.
_GLYPHS = ("✳", "✦", "✳", "✧")
_GLYPH_SECONDS = 0.4
#: Seconds per full breath (dim -> bright -> dim) through the color pulse.
_PULSE_SECONDS = 3.2


def _pulse_color(elapsed: float) -> str:
    """Breathing peri->mauve blend, one full cycle every ``_PULSE_SECONDS``."""
    brightness = 0.5 + 0.5 * math.sin(2 * math.pi * elapsed / _PULSE_SECONDS)
    peri, mauve = _hex_to_rgb(PALETTE["peri"]), _hex_to_rgb(PALETTE["mauve"])
    return _rgb_to_hex(_lerp_rgb(peri, mauve, brightness))


class PulseGlyph(Static):
    """A single-line label whose leading glyph twinkles and breathes color.

    Used both as the "Luna is thinking" placeholder in the chat transcript
    (before the first token of a reply arrives) and as each running tool
    call's row (``ToolRow``) in that same transcript.

    Follows the same pattern as Textual's own ``LoadingIndicator``: driven
    by ``auto_refresh`` + ``render()`` (computed from elapsed wall-clock
    time, not a hand-rolled frame counter) rather than a manual
    ``set_interval``, and it checks ``app.animation_level`` the same way —
    a user who has set ``TEXTUAL_ANIMATIONS=none`` gets a plain static
    label instead of a widget that ignores that preference.
    """

    # Styling (including the animation_level == "none" fallback $peri
    # color — the animated frame overrides it with an explicit Rich style
    # in render()) lives in luna/tui/luna.tcss, not a DEFAULT_CSS string
    # here.

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
        if self.app.animation_level != "none":
            self.auto_refresh = _GLYPH_SECONDS

    def render(self) -> Text:
        """Render the current frame: a static bullet, or the twinkling glyph."""
        label = self.label_text
        if self._started_at is not None:
            label = f"{label} · {time.monotonic() - self._started_at:.0f}s"
        if self.app.animation_level == "none":
            # A static bullet, no cycling glyph or live-updating timer —
            # matches how Textual's own LoadingIndicator falls back to
            # plain "Loading..." text under this same setting.
            return Text(f"⏺ {label}")
        elapsed = time.monotonic() - self._mounted_at
        glyph = _GLYPHS[int(elapsed / _GLYPH_SECONDS) % len(_GLYPHS)]
        return Text(f"{glyph} {label}", style=_pulse_color(elapsed))
