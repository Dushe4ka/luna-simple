"""Detect whether the terminal background is dark (OSC 11, then COLORFGBG)."""

from __future__ import annotations

import os
import re
import select
import sys
import time
from collections.abc import Callable, Mapping

_OSC11_RE = re.compile(
    r"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)",
)
_DA1_RE = re.compile(rb"\x1b\[\?[\d;]*c")
_DARK_INDEXES = {0, 1, 2, 3, 4, 5, 6, 8}
_LIGHT_INDEXES = {7, 15}

_cached: bool | None = None


def parse_osc11(reply: str) -> bool | None:
    """Return True for a dark background, False for light, None if unparsable."""
    match = _OSC11_RE.search(reply)
    if match is None:
        return None
    r, g, b = (int(h, 16) / (16 ** len(h) - 1) for h in match.groups())
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5


def parse_colorfgbg(value: str | None) -> bool | None:
    """Parse ``COLORFGBG`` (``fg;bg``); the last field is the background."""
    if not value:
        return None
    try:
        index = int(value.split(";")[-1])
    except ValueError:
        return None
    if index in _DARK_INDEXES:
        return True
    if index in _LIGHT_INDEXES:
        return False
    return None


def query_osc11(timeout: float = 0.2) -> str | None:
    """Ask the terminal for its background colour; None when impossible.

    A DA1 request is sent right after OSC 11 and used as a sentinel, so a
    slow reply is consumed here rather than leaking into later input.
    """
    try:
        import termios
        import tty
    except ImportError:
        return None
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except (OSError, ValueError, AttributeError):
        return None
    try:
        old = termios.tcgetattr(fd)
    except (OSError, termios.error):
        os.close(fd)
        return None
    buf = b""
    try:
        tty.setraw(fd)
        os.write(fd, b"\x1b]11;?\x07\x1b[c")
        deadline = time.monotonic() + timeout
        while not _DA1_RE.search(buf):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                break
            chunk = os.read(fd, 256)
            if not chunk:
                break
            buf += chunk
    except (OSError, termios.error):
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except (OSError, termios.error):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
    return buf.decode("latin-1") if buf else None


def detect_dark(
    env: Mapping[str, str] | None = None,
    *,
    query: Callable[[], str | None] = query_osc11,
) -> bool:
    """Detect a dark background: OSC 11, then COLORFGBG, then assume dark."""
    global _cached
    if _cached is not None:
        return _cached
    env = os.environ if env is None else env
    result = None
    reply = query()
    if reply:
        result = parse_osc11(reply)
    if result is None:
        result = parse_colorfgbg(env.get("COLORFGBG"))
    _cached = True if result is None else result
    return _cached


def reset_cache() -> None:
    """Forget the cached detection result (for tests)."""
    global _cached
    _cached = None
