"""Auto-format touched files after a mutating turn (best-effort, never raises)."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

_TIMEOUT = 60


def detect(workdir: str) -> str:
    """Guess a formatter command from the project's markers and installed tools."""
    root = Path(workdir)
    if shutil.which("ruff") and (
        (root / "pyproject.toml").is_file() or (root / "ruff.toml").is_file()
    ):
        return "ruff format"
    if shutil.which("black") and (root / "pyproject.toml").is_file():
        return "black -q"
    if shutil.which("prettier") and (root / "package.json").is_file():
        return "prettier -w"
    if shutil.which("gofmt") and (root / "go.mod").is_file():
        return "gofmt -w"
    if shutil.which("rustfmt") and (root / "Cargo.toml").is_file():
        return "rustfmt"
    return ""


def run(command: str, workdir: str, paths: list[str]) -> list[str]:
    """Run ``command`` over ``paths`` (or the whole project when ``paths`` is empty).

    Returns the list of paths the command was pointed at (empty on failure or a
    blank command) so the caller can report how many files were touched.
    """
    if not command.strip():
        return []
    full = f"{command} {' '.join(shlex.quote(f'./{p}') for p in paths)}" if paths else command
    try:
        result = subprocess.run(
            full, shell=True, cwd=workdir, capture_output=True, text=True, timeout=_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return list(paths)
