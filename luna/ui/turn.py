"""Visual framing for a REPL turn: a rule opens Luna's reply, a rule closes it."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from luna.ui.theme import PALETTE


def open_turn(console: Console) -> None:
    """Start Luna's reply with a blank line and a left-aligned rule."""
    console.print()
    console.rule(
        Text("● luna", style=f"bold {PALETTE['peri']}"),
        align="left",
        style=PALETTE["blue"],
    )


def close_turn(console: Console) -> None:
    """End Luna's reply with a thin rule and a blank line."""
    console.rule(style=PALETTE["blue"])
    console.print()
