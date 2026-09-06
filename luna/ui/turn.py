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


def tool_line(console: Console, name: str, summary: str = "") -> None:
    """One dim, indented line describing a tool call/result."""
    text = Text("  ⚙ ", style=PALETTE["blue"])
    text.append(name, style=f"bold {PALETTE['accent']}")
    if summary:
        text.append(f" · {summary}", style=PALETTE["blue"])
    console.print(text)


def close_turn(console: Console) -> None:
    """End Luna's reply with a thin rule and a blank line."""
    console.rule(style=PALETTE["blue"])
    console.print()
