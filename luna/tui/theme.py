"""Textual CSS theme translated from luna/ui/theme.py's PALETTE.

Reuses the existing moon/night palette as-is, with one contrast fix:
persistent secondary text (sidebar labels, timestamps, status bar) uses
``moon_dim`` (9.32:1 contrast against ``bg``) instead of ``blue`` (3.45:1
— below the 4.5:1 minimum for normal text), because unlike the scrolling
REPL, this text sits on screen continuously. ``blue`` remains for borders
and lower-emphasis structural elements. See
docs/superpowers/specs/2026-09-24-luna-tui-design.md's Research section
for the contrast calculation.
"""

from __future__ import annotations

from luna.ui.theme import PALETTE

TUI_CSS_VARIABLES = f"""
$bg: {PALETTE["bg"]};
$moon: {PALETTE["moon"]};
$moon-dim: {PALETTE["moon_dim"]};
$peri: {PALETTE["peri"]};
$border: {PALETTE["blue"]};
$mauve: {PALETTE["mauve"]};
$accent: {PALETTE["accent"]};
"""
