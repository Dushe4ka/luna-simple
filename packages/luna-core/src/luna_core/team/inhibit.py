"""Keep the machine awake while a team runs (``--allow-sleep`` turns it off).

macOS: ``caffeinate -i -w <pid>``; Linux: ``systemd-inhibit`` when present;
Windows: ``SetThreadExecutionState``. Anything missing or failing only
means the machine may sleep: it never stops the run.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator

from luna_core.shell import kill_tree

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _argv(platform: str, which: Callable[[str], str | None]) -> list[str] | None:
    if platform == "darwin":
        exe = which("caffeinate")
        return [exe, "-i", "-w", str(os.getpid())] if exe else None
    exe = which("systemd-inhibit")
    if exe:
        return [exe, "--what=idle:sleep", "--who=luna", "--why=luna team run", "--mode=block",
                "sleep", "infinity"]  # fmt: skip
    return None


def _kernel32() -> object | None:
    try:
        import ctypes

        return ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        return None


@contextlib.contextmanager
def keep_awake(
    enabled: bool = True,
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    kernel32: object | None = None,
) -> Iterator[bool]:
    """Hold off system sleep inside the block; yield whether that worked."""
    if not enabled:
        yield False
        return
    if platform == "win32":
        k32 = kernel32 if kernel32 is not None else _kernel32()
        if k32 is None:
            yield False
            return
        k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        try:
            yield True
        finally:
            k32.SetThreadExecutionState(ES_CONTINUOUS)
        return
    argv = _argv(platform, which)
    if argv is None:
        yield False
        return
    try:
        proc = popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(Exception):
            kill_tree(proc.pid)
            proc.wait(timeout=5)
