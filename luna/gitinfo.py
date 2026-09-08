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
    out = _run(workdir, "status", "--porcelain")
    return [line[3:] for line in out.splitlines() if line.strip()]
