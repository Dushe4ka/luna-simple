"""Luna's TUI color palette, exposed as Textual theme variables.

Reuses the existing moon/night ``PALETTE`` (``luna/ui/theme.py``) as-is,
with one contrast fix: persistent secondary text (sidebar labels,
timestamps, status bar) uses ``moon_dim`` (9.32:1 contrast against ``bg``)
instead of ``blue`` (3.45:1 — below the 4.5:1 minimum for normal text),
because unlike the scrolling REPL, this text sits on screen continuously.
``blue`` remains for borders and lower-emphasis structural elements. See
``docs/superpowers/specs/2026-09-24-luna-tui-design.md``'s Research
section for the contrast calculation.

Exposed via ``LunaApp.get_theme_variable_defaults()`` — Textual's own
mechanism for app-specific ``$variables`` (see the framework's Design
guide) — rather than the previous approach of concatenating a
``$var: value;`` string into every single widget's own ``DEFAULT_CSS``.
That was needed because each widget's ``DEFAULT_CSS`` is parsed as its
own independent stylesheet document — a variable declared only in one
place was invisible everywhere else — but ``get_theme_variable_defaults``
resolves against *every* stylesheet the app loads, inline or external,
without repeating the declaration anywhere.
"""

from __future__ import annotations

from luna.ui.theme import PALETTE

#: ``panel`` is a derived, not-in-``PALETTE`` token: ``bg`` lightened by a
#: 6% white overlay (the standard dark-theme "elevated surface" technique
#: — checked against ui-ux-pro-max's dark-theme reference palettes, which
#: all raise a card/muted surface a comparable amount over their
#: background). Sidebars/status bar use it so the ``border`` line between
#: them and the chat pane sits between two visibly different colors
#: instead of splitting one flat background in two.
TUI_VARIABLES: dict[str, str] = {
    "bg": PALETTE["bg"],
    "panel": "#1a1e33",
    "moon": PALETTE["moon"],
    "moon-dim": PALETTE["moon_dim"],
    "peri": PALETTE["peri"],
    "border": PALETTE["blue"],
    "mauve": PALETTE["mauve"],
    "accent": PALETTE["accent"],
}
