"""The Luna startup splash: an ANSI rendering of the moon artwork."""

from __future__ import annotations

import time

from rich.align import Align
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from luna import __version__

_MOON = [
    "  · · · · · · · · ·  ",
    " ·  ░░▒▒▒▒▒▒▒▒░░   · ",
    "·  ░▒▒▓▓▓▓▓▓▓▓▒▒▒░  ·",
    "· ▒▒▓▓▓▓██▓▓▓▓▓▒▒▒░ ·",
    "·░▒▓▓██▓▓▓▓▓▒▒▓▓▒▒▒░·",
    "·▒▓▓▓▓▓▓▓▒▒▓▓▓▓▒▒▒▒▒·",
    "·▒▓▒▒▓▓▓▓▓▓▓▓▓▓▒▒▓▒▒·",
    "·▒▒▓▓▓▓▓█▓▓▓▓▒▒▓▓▒▒▒·",
    "·░▒▒▓▓▓▓▓▓▓▓▒▒▓▓▒▒▒░·",
    "· ▒▒▒▓▓▓▓▓▓▓▓▓▒▒▒▒░ ·",
    "·  ░▒▒▒▓▓▓▓▓▓▒▒▒▒░  ·",
    " ·   ░░▒▒▒▒▒▒░░    · ",
    "  · · · · · · · · ·  ",
]

_WORDMARK = [
    "█      █ █   █  █    ██ ",
    "█      █ █   ██ █   █  █",
    "█      █ █   █ ██   ████",
    "█      █ █   █  █   █  █",
    "█████  ███   █  █   █  █",
]

_DEFAULT_STEPS = [
    "loading modules ...",
    "connecting to tools ...",
    "preparing your canvas ...",
    "almost there ...",
]

_TAGLINE = "✦  YOUR AI AGENT COMPANION  ✦"
_SUBTITLE = "LUNA — a quiet intelligence for navigating complex systems"
_LOOP = "Observe  •  Understand  •  Plan  •  Act"


def _corners() -> Table:
    grid = Table.grid(expand=True)
    grid.add_column(justify="left")
    grid.add_column(justify="right")
    grid.add_row(
        Text(f"LUNA v{__version__}\nAI AGENT HARNESS\nEXPLORE · PLAN · BUILD", style="luna.slogan"),
        Text("A BRIGHTER\nTOMORROW\nTOGETHER", style="luna.slogan", justify="right"),
    )
    return grid


def _compact(console: Console, steps: list[str]) -> None:
    console.print(Text("LUNA", style="luna.title"))
    console.print(Text(_SUBTITLE, style="luna.tagline"))
    console.print(Text("initializing ...", style="luna.step"))
    for step in steps:
        console.print(Text(f"> {step}", style="luna.step"))


def render_splash(
    console: Console,
    steps: list[str] | None = None,
    *,
    animate: bool = True,
) -> None:
    """Render the splash screen.

    Falls back to a three-line banner on narrow or non-terminal output.
    """
    steps = steps or list(_DEFAULT_STEPS)
    if console.width < 60 or not console.is_terminal:
        _compact(console, steps)
        return

    moon = Text("\n".join(_MOON), style="luna.moon", justify="center")
    wordmark = Text("\n".join(_WORDMARK), style="luna.title", justify="center")
    header = Group(
        _corners(),
        Text(""),
        Align.center(moon),
        Text(""),
        Align.center(wordmark),
        Align.center(Text(_TAGLINE, style="luna.tagline")),
        Align.center(Text(_SUBTITLE, style="luna.slogan")),
        Align.center(Text(_LOOP, style="luna.slogan.accent")),
        Text(""),
        Align.center(Text("INITIALIZING ...", style="luna.step")),
        Text(""),
    )
    console.print(header)

    for step in steps:
        console.print(Text(f"  > {step}", style="luna.step"))
        if animate:
            time.sleep(0.12)
    console.print(
        Align.right(Text("SAME MOON · BRIGHTER POSSIBILITIES", style="luna.slogan.accent"))
    )
    console.print()
