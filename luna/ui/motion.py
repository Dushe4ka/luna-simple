"""Motion helpers: a sine "breath" between two theme colors, and the motion switch."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping

from luna.ui.colors import _hex_to_rgb, _lerp_rgb, _rgb_to_hex

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def breath(elapsed: float, period: float, low: str, high: str, *, floor: float = 0.0) -> str:
    """Blend ``low -> high`` along a sine of ``period`` seconds.

    The brightness is ``floor + (1 - floor) * (0.5 + 0.5 * sin(2π * elapsed / period))``,
    so ``elapsed = period / 4`` is the crest (exactly ``high``). ANSI colors
    cannot be blended: if either color is not ``#rrggbb``, ``high`` is returned.
    """
    if not (_HEX_RE.match(low) and _HEX_RE.match(high)):
        return high
    wave = 0.5 + 0.5 * math.sin(2 * math.pi * elapsed / period)
    brightness = floor + (1.0 - floor) * wave
    return _rgb_to_hex(_lerp_rgb(_hex_to_rgb(low), _hex_to_rgb(high), brightness))


def motion_enabled(env: Mapping[str, str] | None = None, *, setting: bool = True) -> bool:
    """Whether to animate.

    Off for ``setting=False``, ``TEXTUAL_ANIMATIONS=none`` or a non-empty ``NO_MOTION``.
    """
    env = os.environ if env is None else env
    if not setting:
        return False
    if env.get("TEXTUAL_ANIMATIONS", "").strip().lower() == "none":
        return False
    return not env.get("NO_MOTION")
