"""Prompt + guard for `luna init` (generate AGENTS.md)."""

from __future__ import annotations

from pathlib import Path

_BASE = (
    "Explore this repository and {verb} an AGENTS.md at its root. Cover: what the "
    "project does in two sentences; the exact build, test, and lint commands; the "
    "directory/module layout; and the coding conventions a contributor must follow. "
    "Keep it under ~60 lines. Use your read tools first, then write the file."
)


def existing_action(workdir: str) -> str:
    """Return ``"update"`` if ``AGENTS.md`` already exists, else ``"create"``."""
    return "update" if (Path(workdir) / "AGENTS.md").is_file() else "create"


def init_prompt(workdir: str) -> str:
    """Build the natural-language instruction for generating ``AGENTS.md``."""
    if existing_action(workdir) == "update":
        return _BASE.format(verb="revise the existing") + (
            " Preserve anything still accurate; do not blindly overwrite."
        )
    return _BASE.format(verb="create")
