"""The Luna startup splash: a minimal truecolor gradient wordmark.

Same technique real competitor CLIs use (Claude Code, Gemini CLI,
oh-my-logo): a figlet-style block-letter logo with a smooth per-character
truecolor gradient — plain ANSI, no image protocol, no new dependency. No
background scene: essential elements only (wordmark, version, tagline).
"""

from __future__ import annotations

import time

from rich.console import Console
from rich.style import Style
from rich.text import Text

from luna import __version__
from luna.ui.theme import PALETTE

_DEFAULT_STEPS = [
    "loading modules ...",
    "connecting to tools ...",
    "preparing your canvas ...",
    "almost there ...",
]

_TAGLINE = "YOUR AI AGENT COMPANION"
_SUBTITLE = "LUNA - a quiet intelligence for navigating complex systems"
_MAX_WIDTH = 118

RGB = tuple[int, int, int]

# Big "L U N A" wordmark (5 rows), painted with a left-to-right gradient.
_WORDMARK = [
    "█        █    █    █    █     ███  ",
    "█        █    █    █    ██   █   █ ",
    "█        █    █    █ █  █    ██████",
    "█        █    █    █  █ █    █    █",
    "██████    ████     █   ██    █    █",
]
_WORDMARK_GRADIENT = [PALETTE["peri"], PALETTE["moon"], PALETTE["mauve"], PALETTE["accent"]]

# --- Colour math -------------------------------------------------------------


def _hex_to_rgb(hexcolor: str) -> RGB:
    """``"#aabbcc" -> (0xaa, 0xbb, 0xcc)``."""
    h = hexcolor.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgb_to_hex(rgb: RGB) -> str:
    """``(0xaa, 0xbb, 0xcc) -> "#aabbcc"``."""
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _lerp_rgb(a: RGB, b: RGB, t: float) -> RGB:
    """Linear-interpolate between two RGB triples; ``t=0`` is ``a``, ``t=1`` is ``b``."""
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _gradient_stops(colors: list[str], steps: int) -> list[str]:
    """Sample a smooth multi-stop hex gradient at ``steps`` evenly-spaced points.

    ``steps <= 1`` returns just the first color. The endpoints of ``colors``
    are always hit exactly (no interpolation drift at the boundaries).
    """
    if steps <= 1:
        return [colors[0]]
    rgb_stops = [_hex_to_rgb(c) for c in colors]
    segments = len(rgb_stops) - 1
    out: list[str] = []
    for i in range(steps):
        t = i / (steps - 1) * segments
        seg = min(int(t), segments - 1)
        local_t = t - seg
        out.append(_rgb_to_hex(_lerp_rgb(rgb_stops[seg], rgb_stops[seg + 1], local_t)))
    return out


# --- The wordmark -------------------------------------------------------


def _wordmark_lines(width: int) -> list[Text]:
    """Render the block-letter "LUNA" logo, one gradient-colored row per line."""
    inner_width = min(width, _MAX_WIDTH)
    row_width = len(_WORDMARK[0])
    stops = _gradient_stops(_WORDMARK_GRADIENT, row_width)
    pad = " " * max(0, (inner_width - row_width) // 2)
    lines: list[Text] = []
    for row in _WORDMARK:
        text = Text(pad)
        for ch, hexcolor in zip(row, stops, strict=True):
            text.append(ch, style=Style(color=hexcolor))
        lines.append(text)
    return lines


def _centered(width: int, s: str, style: str) -> Text:
    inner_width = min(width, _MAX_WIDTH)
    pad = " " * max(0, (inner_width - len(s)) // 2)
    return Text(pad + s, style=style)


def _compact(console: Console, steps: list[str]) -> None:
    console.print(Text(f"LUNA v{__version__}  ·  {_TAGLINE}", style="luna.title"))
    console.print(Text("INITIALIZING ...", style="luna.step"))
    for step in steps:
        console.print(Text(f"  > {step}", style="luna.step"))
    console.print(Text(_SUBTITLE, style="luna.tagline"))


def render_splash(
    console: Console,
    steps: list[str] | None = None,
    *,
    animate: bool = True,
) -> None:
    """Render the splash screen.

    Falls back to a small banner on narrow or non-terminal output.
    """
    steps = steps or list(_DEFAULT_STEPS)
    if console.width < 88 or not console.is_terminal:
        _compact(console, steps)
        return

    width = console.width
    prefix = Text(" " * max(0, (width - min(width, _MAX_WIDTH)) // 2))

    console.print()
    for line in _wordmark_lines(width):
        console.print(prefix + line)
    console.print()
    console.print(
        prefix + _centered(width, f"v{__version__}  ·  AI AGENT HARNESS", PALETTE["peri"])
    )
    console.print(prefix + _centered(width, _TAGLINE, PALETTE["accent"]))
    console.print()
    console.print(prefix + _centered(width, "INITIALIZING ...", PALETTE["accent"]))
    for step in steps:
        console.print(prefix + _centered(width, f"> {step}", PALETTE["peri"]))
        if animate:
            time.sleep(0.12)
    console.print()
