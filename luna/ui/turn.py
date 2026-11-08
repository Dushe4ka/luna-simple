"""Visual framing for a REPL turn: a rule opens Luna's reply, a rule closes it."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text


def open_turn(console: Console) -> None:
    """Start Luna's reply with a blank line and a left-aligned rule."""
    console.print()
    console.rule(
        Text("● luna", style="luna.prompt"),
        align="left",
        style="luna.border",
    )


def close_turn(console: Console) -> None:
    """End Luna's reply with a thin rule and a blank line."""
    console.rule(style="luna.border")
    console.print()
