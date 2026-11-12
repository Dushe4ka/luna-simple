"""Portable pieces for building shell commands in tests.

Luna runs commands through ``sh``/Git Bash, where an unquoted Windows path loses
its backslashes (``C:\\x\\python.exe`` -> ``C:xpython.exe``). Tests that build a
command line from :data:`sys.executable` or a ``tmp_path`` use these instead.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

PY = shlex.quote(Path(sys.executable).as_posix())
"""The running interpreter, quoted for ``sh``/``bash``."""


def posix(path: str | Path) -> str:
    """``path`` with forward slashes: safe inside a Python or shell string literal."""
    return Path(path).as_posix()
