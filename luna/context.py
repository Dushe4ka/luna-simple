"""``@file`` mention expansion and session-pinned files."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MAX_BYTES", "PinnedFiles", "expand_mentions", "render_pinned"]

MAX_BYTES = 100_000


def _attach(rel: str, path: Path) -> str:
    try:
        data = path.read_bytes()[: MAX_BYTES + 1]
    except OSError:
        return f"\n(@{rel}: unreadable)"
    text = data.decode("utf-8", "replace")
    if len(text) > MAX_BYTES:
        text = text[:MAX_BYTES] + "\n… (truncated)"
    return f"\n\n<attached: {rel}>\n{text}\n</attached>"


def _resolve(token: str, root: Path) -> tuple[str, Path] | None:
    rel = token.lstrip("@")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return rel, candidate


def expand_mentions(text: str, workdir: str) -> str:
    """Append ``<attached:>`` blocks for every ``@path`` token in ``text``."""
    root = Path(workdir)
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    extra = ""
    for tok in tokens:
        if not tok.startswith("@") or len(tok) < 2:
            continue
        resolved = _resolve(tok, root)
        if resolved is None:
            extra += f"\n(@{tok.lstrip('@')}: not found)"
            continue
        rel, path = resolved
        try:
            is_dir = path.is_dir()
            is_file = path.is_file()
        except OSError:
            is_dir = is_file = False
        if is_dir:
            try:
                names = sorted(p.name for p in path.iterdir())
            except OSError:
                names = []
            extra += f"\n\n<dir {rel}>\n" + "\n".join(names) + "\n</dir>"
        elif is_file:
            extra += _attach(rel, path)
        else:
            extra += f"\n(@{rel}: not found)"
    return text + extra


@dataclass
class PinnedFiles:
    """A dedup'd set of session-pinned file paths (relative to the workdir)."""

    _paths: set[str] = field(default_factory=set)

    def add(self, *paths: str) -> None:
        """Pin ``paths`` (already-pinned entries are ignored)."""
        self._paths.update(paths)

    def drop(self, *paths: str) -> None:
        """Unpin ``paths`` (unknown entries are ignored)."""
        self._paths.difference_update(paths)

    @property
    def paths(self) -> list[str]:
        """Sorted list of the currently pinned relative paths."""
        return sorted(self._paths)


def render_pinned(pinned: PinnedFiles, workdir: str) -> str:
    """Render every pinned file, fresh from disk. ``""`` when nothing is pinned."""
    root = Path(workdir)
    out = ""
    for rel in pinned.paths:
        path = root / rel
        try:
            is_file = path.is_file()
        except OSError:
            is_file = False
        if is_file:
            out += _attach(rel, path)
    return out
