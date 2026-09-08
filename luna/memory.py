"""`.luna/memory/*.md` tiers loaded into the system prompt."""

from __future__ import annotations

from datetime import date
from pathlib import Path

KINDS = ("project", "conventions", "decisions", "failures")


def _memory_dir(workdir: str) -> Path:
    return Path(workdir) / ".luna" / "memory"


def memory_files(workdir: str) -> list[str]:
    """Return existing ``.luna/memory/*.md`` as sorted workdir-relative POSIX paths."""
    d = _memory_dir(workdir)
    if not d.is_dir():
        return []
    root = Path(workdir)
    return sorted(str(p.relative_to(root).as_posix()) for p in d.glob("*.md"))


def append_note(workdir: str, kind: str, topic: str, note: str) -> Path:
    """Append a dated section to ``.luna/memory/<kind>.md`` and return its path."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    d = _memory_dir(workdir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{kind}.md"
    section = f"\n## {date.today().isoformat()} — {topic}\n\n{note}\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(section)
    return path
