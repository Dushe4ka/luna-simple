"""Run a project's type/lint checker after edits (best-effort, never raises)."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

_TIMEOUT = 120
_MAX_LINES = 40


def detect(workdir: str) -> str:
    """Guess a diagnostics command from installed tools and project markers."""
    root = Path(workdir)
    if shutil.which("ruff") and (
        (root / "pyproject.toml").is_file() or (root / "ruff.toml").is_file()
    ):
        return "ruff check"
    if shutil.which("pyright") and (root / "pyproject.toml").is_file():
        return "pyright"
    if shutil.which("tsc") and (root / "tsconfig.json").is_file():
        return "tsc --noEmit --pretty false"
    if shutil.which("go") and (root / "go.mod").is_file():
        return "go vet ./..."
    if shutil.which("cargo") and (root / "Cargo.toml").is_file():
        return "cargo check --message-format short"
    return ""


def run(command: str, workdir: str, paths: list[str]) -> str:
    """Run ``command`` and return a capped, best-effort diagnostics summary."""
    if not command.strip():
        return ""
    try:
        executable = shlex.split(command)[0]
    except ValueError:
        return ""
    if not executable or shutil.which(executable) is None:
        return ""
    full = f"{command} {' '.join(paths)}" if paths else command
    try:
        proc = subprocess.run(
            full, shell=True, cwd=workdir, capture_output=True, text=True, timeout=_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode == 0 and not proc.stdout.strip() and not proc.stderr.strip():
        return ""
    combined = (proc.stdout + proc.stderr).splitlines()
    lines = [line for line in combined if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[:_MAX_LINES])
