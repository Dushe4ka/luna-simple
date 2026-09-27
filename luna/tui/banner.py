"""The TUI's startup banner: the moon + LUNA wordmark.

Mounted once as the very first item in the chat transcript. Unlike the
pre-TUI console splash (``luna/ui/splash.py``, a one-shot animated screen
the CLI prints *before* handing off to the full-screen app — gone forever
the moment the TUI takes over), this banner lives *inside* the scrollable
transcript itself. It sits at the top of the conversation, scrolls out of
view as messages accumulate below it, and is still there — unchanged — if
the user scrolls back up. Reuses the CLI splash's own wordmark art rather
than inventing a second logo.
"""

from __future__ import annotations

import math

from rich.align import Align
from rich.console import Group
from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from luna import __version__
from luna.ui.colors import _hex_to_rgb, _lerp_rgb, _rgb_to_hex
from luna.ui.splash import _TAGLINE, _WORDMARK, _WORDMARK_GRADIENT, _gradient_stops
from luna.ui.theme import PALETTE

# --- The moon ------------------------------------------------------------
#
# Built from a real distance-from-center field (not hand-typed rows) so the
# silhouette is a genuinely smooth circle — a first hand-drawn attempt
# using flat ASCII rows with a hard-edged "shadow" rectangle read as a
# glitch, not a moon. Four density bands (█▓▒░, by distance from the
# boundary) anti-alias the round edge against the square character grid;
# `_ASPECT` compensates for terminal cells being taller than they are
# wide, so the result reads as a circle rather than an oval.
_ROWS, _COLS = 13, 31
_ASPECT = 2.05
_RADIUS = (_ROWS - 1) / 2
_BANDS = ((-0.9, "█"), (-0.35, "▓"), (0.15, "▒"), (0.55, "░"))

#: A light source from the upper-left, so the sphere reads as lit rather
#: than flat — brighter toward `peri`/`moon` there, cooler toward `blue`
#: on the shadowed lower-right, entirely via color (no cut-out shape).
_LIGHT = (-0.55, -0.85)
_LIGHT_LEN = math.hypot(*_LIGHT)
_LIT = PALETTE["moon"]
_SHADOW = PALETTE["blue"]


def _moon_glyph_at(dx: float, dy: float) -> str | None:
    """Return the block character for one cell, or ``None`` outside the circle."""
    edge = math.hypot(dx, dy) - _RADIUS
    for threshold, glyph in _BANDS:
        if edge <= threshold:
            return glyph
    return None


def _moon_color_at(nx: float, ny: float) -> str:
    """Lighting-based color for one cell, ``nx``/``ny`` normalized to [-1, 1].

    A cell facing the same direction as ``_LIGHT`` (upper-left) gets a
    *positive* dot product with it — that's the bright side, hence ``0.5 +
    0.5 * dot`` here, not ``0.5 - ...`` (an earlier, inverted version of
    this lit the lower-right instead, the opposite of the intended
    upper-left "light source").
    """
    brightness = 0.5 + 0.5 * ((nx * _LIGHT[0] + ny * _LIGHT[1]) / _LIGHT_LEN)
    brightness = max(0.0, min(1.0, brightness))
    return _rgb_to_hex(_lerp_rgb(_hex_to_rgb(_SHADOW), _hex_to_rgb(_LIT), brightness))


def _moon_lines() -> list[Text]:
    cx, cy = (_COLS - 1) / 2, (_ROWS - 1) / 2
    lines = []
    for y in range(_ROWS):
        text = Text()
        for x in range(_COLS):
            dx, dy = (x - cx) / _ASPECT, y - cy
            glyph = _moon_glyph_at(dx, dy)
            if glyph is None:
                text.append(" ")
            else:
                color = _moon_color_at(dx / _RADIUS, dy / _RADIUS)
                text.append(glyph, style=Style(color=color))
        lines.append(text)
    return lines


# --- The wordmark ----------------------------------------------------------


def _wordmark_lines() -> list[Text]:
    """LUNA's block-letter wordmark, reusing the console splash's own art."""
    stops = _gradient_stops(_WORDMARK_GRADIENT, len(_WORDMARK[0]))
    lines = []
    for row in _WORDMARK:
        text = Text()
        for ch, color in zip(row, stops, strict=True):
            text.append(ch, style=Style(color=color))
        lines.append(text)
    return lines


class LunaBanner(Static):
    """The moon + wordmark, shown once at the top of a fresh TUI session.

    Styling lives in ``luna/tui/luna.tcss`` (external stylesheet), not a
    ``DEFAULT_CSS`` string here.
    """

    def render(self) -> Group:
        """Compose the moon glyph, wordmark, version, and tagline into one group.

        Every line is wrapped in its own ``Align.center`` rather than
        relying on ``Text(justify="center")``: Rich only centers a
        renderable within space *wider than its own measured content* —
        printed bare, a ``Text`` measures at exactly its own length, so
        `justify` had no width to center within and every row (each with a
        slightly different amount of trailing whitespace baked into the
        block-art string) rendered flush left at a *different* effective
        offset, reading as misaligned, jumbled letters. `Align.center`
        forces each line to expand to the panel's actual width first, so
        every row centers against the same reference and lines up.
        """
        lines = [
            *_moon_lines(),
            Text(),
            *_wordmark_lines(),
            Text(),
            Text(f"v{__version__}  ·  AI AGENT HARNESS", style=PALETTE["peri"]),
            Text(_TAGLINE, style=PALETTE["accent"]),
        ]
        return Group(*(Align.center(line) for line in lines))
