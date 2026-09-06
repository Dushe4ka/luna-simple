"""The Luna startup splash: an ANSI night-sky scene rendered from the artwork."""

from __future__ import annotations

import math
import random
import time

from rich.console import Console
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

# Big "L U N A" wordmark (5 rows).
_WORDMARK = [
    "█        █    █    █    █     ███  ",
    "█        █    █    ██   █    █   █ ",
    "█        █    █    █ █  █    ██████",
    "█        █    █    █  █ █    █    █",
    "██████    ████     █   ██    █    █",
]

# Small constellations: node (x, y) offsets and the edges (index pairs).
_CONSTELLATION_L = (
    [(0, 2), (4, 0), (8, 3), (12, 1), (10, 6), (15, 5)],
    [(0, 1), (1, 2), (2, 3), (2, 4), (4, 5)],
)
_CONSTELLATION_R = (
    [(0, 0), (4, 3), (9, 2), (13, 5), (17, 3), (20, 6)],
    [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)],
)


class _Canvas:
    """A grid of styled characters that layers paint on top of paint."""

    def __init__(self, width: int, height: int) -> None:
        self.w = width
        self.h = height
        self._cells: list[list[tuple[str, str | None]]] = [
            [(" ", None) for _ in range(width)] for _ in range(height)
        ]

    def put(self, x: int, y: int, ch: str, style: str | None = None) -> None:
        if 0 <= y < self.h and 0 <= x < self.w and ch != " ":
            self._cells[y][x] = (ch, style)

    def clear_rect(self, x: int, y: int, w: int, h: int) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if 0 <= yy < self.h and 0 <= xx < self.w:
                    self._cells[yy][xx] = (" ", None)

    def label(self, x: int, y: int, s: str, style: str | None = None) -> None:
        self.clear_rect(x - 1, y, len(s) + 2, 1)
        for i, ch in enumerate(s):
            self.put(x + i, y, ch, style)

    def center(self, y: int, s: str, style: str | None = None) -> None:
        self.label((self.w - len(s)) // 2, y, s, style)

    def lines(self) -> list[Text]:
        rows: list[Text] = []
        for row in self._cells:
            line = Text()
            for ch, style in row:
                line.append(ch, style=style)
            rows.append(line)
        return rows


def _draw_line(cv: _Canvas, x0: int, y0: int, x1: int, y1: int, ch: str, style: str) -> None:
    steps = max(abs(x1 - x0), abs(y1 - y0)) or 1
    for i in range(steps + 1):
        cv.put(round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps), ch, style)


def _starfield(cv: _Canvas, rnd: random.Random, cx: int, cy: int, rx: int, ry: int) -> None:
    glyphs = [
        (".", PALETTE["blue"]),
        ("·", PALETTE["peri"]),
        ("+", PALETTE["peri"]),
        ("✦", PALETTE["accent"]),
        ("*", PALETTE["moon_dim"]),
        ("✧", PALETTE["moon"]),
    ]
    for _ in range(int(cv.w * 1.15)):
        x = rnd.randrange(cv.w)
        y = rnd.randrange(1, cv.h - 8)
        if ((x - cx) / (rx + 3)) ** 2 + ((y - cy) / (ry + 2)) ** 2 < 1:
            continue
        ch, style = glyphs[min(len(glyphs) - 1, int(rnd.random() ** 2 * len(glyphs)))]
        cv.put(x, y, ch, style)


def _constellation(cv: _Canvas, data, x0: int, y0: int) -> None:
    nodes, edges = data
    for a, b in edges:
        ax, ay = nodes[a]
        bx, by = nodes[b]
        _draw_line(cv, x0 + ax, y0 + ay, x0 + bx, y0 + by, "·", PALETTE["blue"])
    for nx, ny in nodes:
        cv.put(x0 + nx, y0 + ny, "✦", PALETTE["accent"])


def _comet(cv: _Canvas, hx: int, hy: int) -> None:
    cv.put(hx, hy, "✦", PALETTE["moon"])
    for i in range(1, 13):
        style = PALETTE["accent"] if i < 5 else PALETTE["peri"] if i < 9 else PALETTE["blue"]
        cv.put(hx + i, hy - (i // 2), "╱", style)


def _moon(cv: _Canvas, cx: int, cy: int, rx: int, ry: int) -> None:
    craters = [(-6, -2), (4, -3), (-2, 1), (7, 2), (-9, 2), (2, 4), (9, -1), (-4, -4)]
    for y in range(cy - ry - 2, cy + ry + 3):
        for x in range(cx - rx - 4, cx + rx + 5):
            d = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2
            if d <= 1.0:
                cv.put(x, y, "▓" if d > 0.86 else "█", PALETTE["moon"])
            elif d <= 1.26 and (x + y) % 2 == 0:
                cv.put(x, y, "░", PALETTE["moon_dim"])
    for dx, dy in craters:
        cv.put(cx + dx, cy + dy, "▓", PALETTE["moon_dim"])
    for deg in range(0, 360, 7):
        a = math.radians(deg)
        cv.put(
            round(cx + math.cos(a) * (rx + 3)),
            round(cy + math.sin(a) * (ry + 2)),
            "·",
            PALETTE["peri"],
        )


def _clouds(cv: _Canvas, rnd: random.Random, y0: int, *, left: bool) -> None:
    for i, wdt in enumerate((7, 13, 19, 22, 16, 9)):
        y = y0 + i
        xs = range(wdt) if left else range(cv.w - wdt, cv.w)
        for j, x in enumerate(xs):
            edge = j < 2 or j > wdt - 3
            cv.put(
                x,
                y,
                "░" if edge else rnd.choice(["▓", "▒", "▓"]),
                PALETTE["blue"] if edge else PALETTE["mauve"],
            )


def _horizon(cv: _Canvas, cx: int) -> None:
    ridge = cv.h - 2
    blocks = " ▁▂▃▄▅"
    for x in range(cv.w):
        p = 0.5 + 0.5 * math.sin(x * 0.17) * math.cos(x * 0.09)
        cv.put(x, ridge, blocks[1 + int(p * 4)], "#2b3566")
    sea = cv.h - 1
    for x in range(cv.w):
        if cx - 4 <= x <= cx + 4:
            cv.put(x, sea, "▒", PALETTE["moon_dim"])
        elif x % 3 == 0:
            cv.put(x, sea, "░", PALETTE["blue"])


def _paint_scene(width: int) -> list[Text]:
    w = min(width, _MAX_WIDTH)
    h = 36
    cv = _Canvas(w, h)
    cx, cy, rx, ry = w // 2, 10, 16, 6
    rnd = random.Random(20)

    _starfield(cv, rnd, cx, cy, rx, ry)
    _comet(cv, min(w - 20, cx + 18), 6)
    _constellation(cv, _CONSTELLATION_L, 5, 6)
    _constellation(cv, _CONSTELLATION_R, w - 27, 7)
    _moon(cv, cx, cy, rx, ry)
    _clouds(cv, rnd, h - 17, left=True)
    _clouds(cv, rnd, h - 17, left=False)
    _horizon(cv, cx)

    cv.label(0, 0, f"LUNA v{__version__}", PALETTE["peri"])
    cv.label(0, 1, "AI AGENT HARNESS", PALETTE["peri"])
    cv.label(0, 2, "EXPLORE • PLAN • BUILD • TOGETHER", PALETTE["blue"])
    for i, s in enumerate(("A BRIGHTER", "TOMORROW", "TOGETHER")):
        cv.label(w - len(s), i, s, PALETTE["peri"])
    for i, s in enumerate(("IDEAS", "INTO", "REALITY")):
        cv.label(2, 9 + i * 2, s, PALETTE["blue"])
    for i, s in enumerate(("HUMAN", "AND AI", "FURTHER", "TOGETHER")):
        cv.label(w - len(s) - 2, 8 + i * 2, s, PALETTE["blue"])

    wy = h - 11
    cv.center(wy - 1, "·   ˖   ✦   ˖   ·", PALETTE["mauve"])
    for i, row in enumerate(_WORDMARK):
        cv.center(wy + i, row, PALETTE["moon"])
    cv.center(wy + 5, f"✦    {_TAGLINE}    ✦", PALETTE["peri"])
    cv.center(wy + 7, "INITIALIZING ...", PALETTE["accent"])

    return cv.lines()


def _closing(w: int) -> list[Text]:
    pad = " " * max(0, min(w, _MAX_WIDTH) - 13)
    return [
        Text(pad + s, style=PALETTE["mauve"]) for s in ("SAME MOON", "BRIGHTER", "POSSIBILITIES")
    ]


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

    prefix = Text(" " * max(0, (console.width - min(console.width, _MAX_WIDTH)) // 2))
    for line in _paint_scene(console.width):
        console.print(prefix + line)
    console.print()
    for step in steps:
        console.print(prefix + Text(f"  > {step}", style=PALETTE["peri"]))
        if animate:
            time.sleep(0.12)
    for line in _closing(console.width):
        console.print(prefix + line)
    console.print()
