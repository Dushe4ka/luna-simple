"""The Luna startup splash: a truecolor night-sky scene rendered from the artwork.

Two techniques carry the visual weight, both plain ANSI (no image protocol,
no new dependency): a half-block pixel canvas (the ``▀`` character gives two
independently-colored logical pixels per terminal cell — top pixel as the
foreground, bottom pixel as the background), and smooth RGB gradients
everywhere a real scene would have one (sky, moon, water, clouds, wordmark)
instead of a small named-color palette applied per glyph.
"""

from __future__ import annotations

import math
import random
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
    "█        █    █    ██   █    █   █ ",
    "█        █    █    █ █  █    ██████",
    "█        █    █    █  █ █    █    █",
    "██████    ████     █   ██    █    █",
]
_WORDMARK_GRADIENT = [PALETTE["peri"], PALETTE["moon"], PALETTE["mauve"], PALETTE["accent"]]

# Small constellations: node (x, y) offsets and the edges (index pairs).
_CONSTELLATION_L = (
    [(0, 2), (4, 0), (8, 3), (12, 1), (10, 6), (15, 5)],
    [(0, 1), (1, 2), (2, 3), (2, 4), (4, 5)],
)
_CONSTELLATION_R = (
    [(0, 0), (4, 3), (9, 2), (13, 5), (17, 3), (20, 6)],
    [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)],
)

# --- Colour math ------------------------------------------------------------


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


# --- The half-block pixel scene ---------------------------------------------


class _Scene:
    """A night-sky scene: an RGB pixel grid plus a crisp text overlay.

    The pixel grid renders at 2x the terminal's vertical resolution via
    ``▀`` half-blocks. Background painting works in logical pixel
    coordinates (``0..width-1``,
    ``0..height*2-1``). Text overlays work in terminal-row coordinates
    (``0..height-1``) and blend into whatever pixel color is already behind
    them, so labels read as part of the scene rather than sitting in a flat
    black box.
    """

    def __init__(self, width: int, rows: int, bg: RGB) -> None:
        self.w = width
        self.rows = rows
        self.h = rows * 2
        self._px: list[list[RGB]] = [[bg] * width for _ in range(self.h)]
        self._overlay: list[list[tuple[str, str] | None]] = [[None] * width for _ in range(rows)]

    # -- background pixels --

    def set_pixel(self, x: int, y: int, rgb: RGB) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self._px[y][x] = rgb

    def blend_pixel(self, x: int, y: int, rgb: RGB, alpha: float) -> None:
        if 0 <= x < self.w and 0 <= y < self.h and alpha > 0:
            self._px[y][x] = _lerp_rgb(self._px[y][x], rgb, min(1.0, alpha))

    def pixel_at(self, x: int, y: int) -> RGB:
        x = max(0, min(self.w - 1, x))
        y = max(0, min(self.h - 1, y))
        return self._px[y][x]

    # -- text overlay --

    def put_text(self, row: int, col: int, ch: str, fg_hex: str) -> None:
        if 0 <= row < self.rows and 0 <= col < self.w:
            self._overlay[row][col] = (ch, fg_hex)

    def label(self, row: int, col: int, s: str, fg_hex: str) -> None:
        # Spaces are overlaid too (not skipped): a label is a solid strip of
        # real text sitting over the scene, so internal spaces must render
        # as literal blank cells — not fall through to a "▀" scene pixel,
        # which would corrupt both the visual and any plain-text match.
        for i, ch in enumerate(s):
            self.put_text(row, col + i, ch, fg_hex)

    def label_gradient(self, row: int, col: int, s: str, colors: list[str]) -> None:
        """Like :meth:`label`, but with a left-to-right gradient per character."""
        stops = _gradient_stops(colors, max(1, len(s)))
        for i, ch in enumerate(s):
            self.put_text(row, col + i, ch, stops[i])

    def center(self, row: int, s: str, fg_hex: str) -> None:
        self.label(row, (self.w - len(s)) // 2, s, fg_hex)

    def center_gradient(self, row: int, s: str, colors: list[str]) -> None:
        self.label_gradient(row, (self.w - len(s)) // 2, s, colors)

    # -- render --

    def render(self) -> list[Text]:
        lines: list[Text] = []
        for r in range(self.rows):
            top, bot = self._px[r * 2], self._px[r * 2 + 1]
            line = Text()
            overlay_row = self._overlay[r]
            for x in range(self.w):
                ov = overlay_row[x]
                bg_hex = _rgb_to_hex(bot[x])
                if ov is not None:
                    ch, fg_hex = ov
                    line.append(ch, style=Style(color=fg_hex, bgcolor=bg_hex))
                else:
                    line.append("▀", style=Style(color=_rgb_to_hex(top[x]), bgcolor=bg_hex))
            lines.append(line)
        return lines


# --- Scene painters -----------------------------------------------------


def _sky(scene: _Scene, top_hex: str, bottom_hex: str) -> None:
    top_rgb, bottom_rgb = _hex_to_rgb(top_hex), _hex_to_rgb(bottom_hex)
    for y in range(scene.h):
        t = y / max(1, scene.h - 1)
        rgb = _lerp_rgb(top_rgb, bottom_rgb, t)
        for x in range(scene.w):
            scene.set_pixel(x, y, rgb)


def _starfield(scene: _Scene, rnd: random.Random, cx: int, cy: int, rx: int, ry: int) -> None:
    glyph_colors = [
        PALETTE["blue"],
        PALETTE["peri"],
        PALETTE["accent"],
        PALETTE["moon_dim"],
        PALETTE["moon"],
    ]
    for _ in range(int(scene.w * scene.h * 0.03)):
        x = rnd.randrange(scene.w)
        y = rnd.randrange(2, scene.h - 14)
        if ((x - cx) / (rx * 2 + 6)) ** 2 + ((y - cy) / (ry * 2 + 4)) ** 2 < 1:
            continue
        color = glyph_colors[min(len(glyph_colors) - 1, int(rnd.random() ** 2 * len(glyph_colors)))]
        brightness = 0.5 + 0.5 * rnd.random()
        scene.blend_pixel(x, y, _hex_to_rgb(color), brightness)


def _sparkles(scene: _Scene, rnd: random.Random, cx: int, cy: int, r: int) -> None:
    """Scatter a few crisp glyph "twinkle" stars over the smooth pixel starfield.

    The soft gradient sky sells the painterly look, but a few sharp accent
    points are what actually read as "stars" up close.
    """
    glyphs = [("✦", PALETTE["accent"]), ("✧", PALETTE["moon"]), ("+", PALETTE["peri"])]
    for _ in range(int(scene.rows * scene.w * 0.006)):
        row = rnd.randrange(1, scene.rows - 12)
        col = rnd.randrange(scene.w)
        if ((col - cx) / (r * 2 + 3)) ** 2 + ((row * 2 - cy) / (r * 2 + 3)) ** 2 < 1:
            continue
        glyph, color = rnd.choice(glyphs)
        scene.put_text(row, col, glyph, color)


def _constellation(scene: _Scene, data, x0: int, y0: int) -> None:
    nodes, edges = data
    line_rgb = _hex_to_rgb(PALETTE["blue"])
    node_rgb = _hex_to_rgb(PALETTE["accent"])
    for a, b in edges:
        ax, ay = nodes[a]
        bx, by = nodes[b]
        x0p, y0p, x1p, y1p = x0 + ax * 2, y0 + ay * 2, x0 + bx * 2, y0 + by * 2
        steps = max(abs(x1p - x0p), abs(y1p - y0p)) or 1
        for i in range(steps + 1):
            x = round(x0p + (x1p - x0p) * i / steps)
            y = round(y0p + (y1p - y0p) * i / steps)
            scene.blend_pixel(x, y, line_rgb, 0.6)
    for nx, ny in nodes:
        scene.set_pixel(x0 + nx * 2, y0 + ny * 2, node_rgb)


def _comet(scene: _Scene, hx: int, hy: int) -> None:
    head = _hex_to_rgb(PALETTE["moon"])
    scene.set_pixel(hx, hy, head)
    tail_colors = [PALETTE["accent"], PALETTE["peri"], PALETTE["blue"]]
    for i in range(1, 20):
        color = tail_colors[min(len(tail_colors) - 1, i // 7)]
        alpha = max(0.15, 1.0 - i / 20)
        scene.blend_pixel(hx + i, hy - i // 2, _hex_to_rgb(color), alpha)


def _moon(scene: _Scene, cx: int, cy: int, r: int) -> None:
    base = _hex_to_rgb(PALETTE["moon_dim"])
    glow = _hex_to_rgb(PALETTE["moon"])
    halo = _hex_to_rgb(PALETTE["accent"])
    craters = [
        (-11, -4, 3),
        (7, -6, 4),
        (-4, 2, 2),
        (12, 4, 3),
        (-16, 4, 3),
        (4, 8, 2),
        (16, -2, 2),
    ]
    crater_rgb = _hex_to_rgb(PALETTE["blue"])

    for y in range(cy - r - 10, cy + r + 10):
        for x in range(cx - r - 10, cx + r + 10):
            dx, dy = x - cx, y - cy
            d = math.hypot(dx, dy) / r
            if d <= 1.0:
                # a soft light source toward the upper-left of the disc
                lx, ly = dx / r + 0.35, dy / r + 0.45
                light = max(0.0, 1.0 - math.hypot(lx, ly))
                shade = 0.55 + 0.45 * light - 0.15 * d**2
                scene.set_pixel(x, y, _lerp_rgb(base, glow, max(0.0, min(1.0, shade))))
            elif d <= 1.5:
                alpha = max(0.0, 1.0 - (d - 1.0) / 0.5) ** 2 * 0.55
                scene.blend_pixel(x, y, halo, alpha)

    for dx, dy, cr in craters:
        for y in range(cy + dy - cr, cy + dy + cr + 1):
            for x in range(cx + dx - cr, cx + dx + cr + 1):
                d = math.hypot(x - (cx + dx), y - (cy + dy)) / cr
                if d <= 1.0 and math.hypot(x - cx, y - cy) <= r:
                    scene.blend_pixel(x, y, crater_rgb, (1 - d) * 0.3)


def _cloud_blob(scene: _Scene, cx: int, cy: int, rx: int, ry: int, rgb: RGB, alpha: float) -> None:
    for y in range(cy - ry, cy + ry + 1):
        for x in range(cx - rx, cx + rx + 1):
            d = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2
            if d <= 1.0:
                scene.blend_pixel(x, y, rgb, alpha * (1 - d) ** 0.6)


def _clouds(scene: _Scene, rnd: random.Random, y0: int, *, left: bool) -> None:
    mauve, blue = _hex_to_rgb(PALETTE["mauve"]), _hex_to_rgb(PALETTE["blue"])
    x_edge = 0 if left else scene.w
    step = 1 if left else -1
    for i, (rx, ry) in enumerate([(10, 5), (16, 7), (22, 8), (17, 6), (11, 4)]):
        cx = x_edge + step * (rx - 4 + i * 6)
        cy = y0 + i * 3 + rnd.randint(-1, 1)
        _cloud_blob(scene, cx, cy, rx, ry, mauve, 0.5)
        _cloud_blob(scene, cx - step * 3, cy - 1, rx - 4, ry - 2, blue, 0.3)


def _horizon(scene: _Scene, cx: int, sky_bottom_hex: str) -> None:
    ridge_rgb = _hex_to_rgb("#1c2148")
    rim_rgb = _hex_to_rgb(PALETTE["mauve"])
    water_rgb = _hex_to_rgb("#11142c")
    glow_rgb = _hex_to_rgb(PALETTE["moon"])

    # A rolling hill skyline (two sine harmonics keep it irregular but
    # smooth) with a warm rim-light catching its topmost pixel, like a
    # moonlit ridge — this replaces a single flat "ground" row.
    base_row = scene.h - 16
    heights = [
        base_row - round(3 * math.sin(x * 0.05) + 1.5 * math.sin(x * 0.13 + 1.7))
        for x in range(scene.w)
    ]
    for x, ridge_top in enumerate(heights):
        scene.set_pixel(x, ridge_top, _lerp_rgb(ridge_rgb, rim_rgb, 0.65))
        scene.set_pixel(x, ridge_top + 1, _lerp_rgb(ridge_rgb, rim_rgb, 0.25))
        for y in range(ridge_top + 2, scene.h):
            t = (y - ridge_top) / max(1, scene.h - 1 - ridge_top)
            scene.set_pixel(x, y, _lerp_rgb(ridge_rgb, water_rgb, min(1.0, t * 1.6)))

    # The moon's reflection: a soft, rippled glow column with smooth
    # Gaussian falloff in both axes — no hard edges, unlike a fixed-width
    # taper would give.
    water_top = min(heights)
    for y in range(water_top + 2, scene.h):
        t = (y - water_top) / max(1, scene.h - 1 - water_top)
        ripple = round(math.sin(y * 0.7) * 3)
        depth_falloff = math.exp(-t * 1.1)
        for dx in range(-14, 15):
            width_falloff = math.exp(-((dx / 9) ** 2))
            scene.blend_pixel(cx + dx + ripple, y, glow_rgb, width_falloff * depth_falloff * 0.75)


def _paint_scene(width: int) -> list[Text]:
    w = min(width, _MAX_WIDTH)
    rows = 36
    scene = _Scene(w, rows, _hex_to_rgb(PALETTE["bg"]))
    cx, cy, r = w // 2, 22, 15
    rnd = random.Random(20)

    _sky(scene, "#0b1026", "#161c3d")
    _starfield(scene, rnd, cx, cy, r, r)
    _sparkles(scene, rnd, cx, cy, r)
    _comet(scene, min(w - 20, cx + 36), 12)
    _constellation(scene, _CONSTELLATION_L, 5, 12)
    _constellation(scene, _CONSTELLATION_R, w - 27, 14)
    _clouds(scene, rnd, scene.h - 34, left=True)
    _clouds(scene, rnd, scene.h - 34, left=False)
    _horizon(scene, cx, "#161c3d")
    _moon(scene, cx, cy, r)

    scene.label(0, 0, f"LUNA v{__version__}", PALETTE["peri"])
    scene.label(1, 0, "AI AGENT HARNESS", PALETTE["peri"])
    scene.label(2, 0, "EXPLORE • PLAN • BUILD • TOGETHER", PALETTE["blue"])
    for i, s in enumerate(("A BRIGHTER", "TOMORROW", "TOGETHER")):
        scene.label(i, w - len(s), s, PALETTE["peri"])
    for i, s in enumerate(("IDEAS", "INTO", "REALITY")):
        scene.label(9 + i * 2, 2, s, PALETTE["blue"])
    for i, s in enumerate(("HUMAN", "AND AI", "FURTHER", "TOGETHER")):
        scene.label(8 + i * 2, w - len(s) - 2, s, PALETTE["blue"])

    wy = rows - 11
    scene.center(wy - 1, "·   ˖   ✦   ˖   ·", PALETTE["mauve"])
    for i, row in enumerate(_WORDMARK):
        scene.center_gradient(wy + i, row, _WORDMARK_GRADIENT)
    scene.center(wy + 5, f"✦    {_TAGLINE}    ✦", PALETTE["peri"])
    scene.center(wy + 7, "INITIALIZING ...", PALETTE["accent"])

    return scene.render()


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
