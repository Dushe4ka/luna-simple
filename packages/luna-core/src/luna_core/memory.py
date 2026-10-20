"""Durable notes in ``.luna/memory/<kind>.md`` and the ``remember`` tool."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from luna_core.tools.base import ToolContext, ToolOutput, tool

KINDS = ("project", "conventions", "decisions", "failures")


def _dir(root: str | Path) -> Path:
    return Path(root) / ".luna" / "memory"


def memory_files(root: str | Path) -> list[str]:
    """Existing memory files as sorted root-relative POSIX paths."""
    d = _dir(root)
    if not d.is_dir():
        return []
    return sorted(p.relative_to(Path(root)).as_posix() for p in d.glob("*.md"))


def append_note(root: str | Path, kind: str, topic: str, note: str) -> Path:
    """Append a dated section to ``.luna/memory/<kind>.md``."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    path = _dir(root) / f"{kind}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {date.today().isoformat()} — {topic}\n\n{note}\n")
    return path


@tool(mutating=True)
async def remember(
    ctx: ToolContext,
    kind: Literal["project", "conventions", "decisions", "failures"],
    topic: str,
    note: str,
) -> ToolOutput:
    """Save a durable note for future sessions (failures = dead ends not to retry).

    Args:
        ctx: Tool context; the note is written under ``ctx.workspace.root``.
        kind: project, conventions, decisions or failures.
        topic: Short heading.
        note: The note itself.

    """
    path = append_note(ctx.workspace.root, kind, topic, note)
    rel = path.relative_to(ctx.workspace.root).as_posix()
    return ToolOutput.text(
        f"noted in {rel}; it is loaded into context from the next session (or /reload)",
        f"noted {kind}",
    )
