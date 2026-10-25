"""Run the project's verify command and capture a short tail."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable

from luna_core.sandbox import scrub_env

VERIFY_TAIL_LINES = 40
_TIMEOUT = 300


def run_verify(command: str, workdir: str, *, pass_env: Iterable[str] = ()) -> tuple[bool, str]:
    """Run ``command`` in ``workdir``; return ``(ok, tail)``.

    ``tail`` is the last :data:`VERIFY_TAIL_LINES` lines of combined
    stdout+stderr. An empty command is treated as disabled -> ``(True, "")``.
    This never raises: timeouts and OS errors return ``(False, message)``.
    Secret variables are removed from the command's environment except
    ``pass_env`` (``[execute] pass_env``).
    """
    if not command.strip():
        return True, ""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env=scrub_env(os.environ, passthrough=pass_env),
        )
    except subprocess.TimeoutExpired:
        return False, f"verify command timed out after {_TIMEOUT}s"
    except OSError as exc:
        return False, f"could not run verify command: {exc}"
    combined = (proc.stdout + proc.stderr).splitlines()
    tail = "\n".join(combined[-VERIFY_TAIL_LINES:])
    return proc.returncode == 0, tail
