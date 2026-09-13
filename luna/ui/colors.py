"""Shared RGB color math for Luna's truecolor UI (splash wordmark, tool progress pulse).

Pure functions, no rendering, no rich dependency.
"""

from __future__ import annotations

RGB = tuple[int, int, int]


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
