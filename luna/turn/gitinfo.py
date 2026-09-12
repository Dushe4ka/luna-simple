"""Read-only git status helpers (never raise)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(workdir: str, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=workdir, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _run_raw(workdir: str, *args: str) -> str:
    """Like :func:`_run` but never strips stdout.

    ``git status --porcelain -z`` output is NUL-delimited and its first two
    columns are a fixed-width status code that can legitimately start with a
    space (e.g. ``" M"`` for "modified, unstaged"); ``str.strip()`` treats
    that leading space as whitespace to discard, which would corrupt the
    first entry's status/path split.
    """
    try:
        out = subprocess.run(["git", *args], cwd=workdir, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def is_git_repo(workdir: str) -> bool:
    """Report whether *workdir* itself is a repo root (not an ancestor repo)."""
    try:
        base = Path(workdir).resolve()
    except OSError:
        return False
    if (base / ".git").exists():
        return True
    top = _run(workdir, "rev-parse", "--show-toplevel")
    if not top:
        return False
    try:
        return Path(top).resolve() == base
    except OSError:
        return False


def dirty_paths(workdir: str) -> list[str]:
    """Return the paths git reports as changed, or ``[]`` outside a repo."""
    if not is_git_repo(workdir):
        return []
    out = _run_raw(workdir, "status", "--porcelain", "-z")
    parts = out.split("\0") if out else []
    paths: list[str] = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if status[0] in ("R", "C"):
            i += 1  # skip the "from" path that -z appends after a rename/copy record
    return paths
