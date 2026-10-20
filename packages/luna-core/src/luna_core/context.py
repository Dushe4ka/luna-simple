"""System prompt assembly and ``@path`` mentions."""

from __future__ import annotations

import base64
import datetime
import platform
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from luna_core.memory import memory_files
from luna_core.messages import Image
from luna_core.skills import SkillInfo
from luna_core.workspace import InvalidPath, OutsideWorkspace, Workspace

MAX_ATTACH = 100_000
MAX_MEMORY = 20_000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

DEFAULT_SYSTEM_PROMPT = """\
You are a coding agent working in the user's repository through file and shell tools.
Read before you write, make the smallest change that achieves the goal, run the
project's tests when they exist, and report plainly what you did and what failed.
Paths are relative to the project root; you cannot touch files outside it."""


def _read(path: Path, limit: int) -> str:
    """Read at most ``limit`` characters, never loading a larger file whole."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            text = fh.read(limit + 1)
    except OSError:
        return ""
    return text[:limit] + "\n… (truncated)" if len(text) > limit else text


def build_system_prompt(
    base: str,
    *,
    root: str | Path,
    skills: list[SkillInfo] | tuple = (),
    memory: list[str] | None = None,
    extra: str = "",
    now: datetime.date | None = None,
) -> str:
    """Assemble the system prompt (stable parts first for prompt caching)."""
    root = Path(root)
    today = (now or datetime.date.today()).isoformat()
    env = (
        f"# Environment\n\nWorking directory: {root}\n"
        f"Platform: {platform.system()} {platform.machine()}\nDate: {today}"
    )
    parts = [base.rstrip(), env]
    agents = root / "AGENTS.md"
    if agents.is_file():
        parts.append(f"# Project instructions (AGENTS.md)\n\n{_read(agents, MAX_MEMORY)}")
    files = memory_files(root) if memory is None else memory
    if files:
        notes = "\n\n".join(f"## {rel}\n{_read(root / rel, MAX_MEMORY)}" for rel in files)
        parts.append(f"# Memory\n\n{notes}")
    if skills:
        index = "\n".join(f"- {s.name}: {s.description}" for s in skills)
        parts.append(f"# Skills\n\nLoad one with the `skill` tool before following it.\n{index}")
    if extra:
        parts.append(extra.strip())
    return "\n\n".join(parts) + "\n"


def expand_mentions(text: str, workspace: Workspace) -> tuple[str, list[Image]]:
    """Attach ``@path`` files and directories; image files become image blocks."""
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    extra = ""
    images: list[Image] = []
    for token in tokens:
        if not token.startswith("@") or len(token) < 2:
            continue
        rel = token[1:]
        try:
            path = workspace.resolve(rel)
        except OutsideWorkspace:
            extra += f"\n(@{rel}: outside the project)"
            continue
        except InvalidPath:
            extra += f"\n(@{rel}: invalid path)"
            continue
        if path.is_dir():
            names = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
            extra += f"\n\n<dir {rel}>\n" + "\n".join(names) + "\n</dir>"
        elif path.is_file() and path.suffix.lower() in _IMAGES:
            size = path.stat().st_size
            if size > MAX_IMAGE_BYTES:
                extra += f"\n(@{rel}: image too large, {size / 1_048_576:.1f} MB)"
            else:
                images.append(
                    Image(
                        _IMAGES[path.suffix.lower()], base64.b64encode(path.read_bytes()).decode()
                    )
                )
                extra += f"\n<image: {rel}>"
        elif path.is_file():
            extra += f"\n\n<attached: {rel}>\n{_read(path, MAX_ATTACH)}\n</attached>"
        else:
            extra += f"\n(@{rel}: not found)"
    return text + extra, images


@dataclass
class PinnedFiles:
    """Files re-attached, fresh from disk, to every turn."""

    _paths: set[str] = field(default_factory=set)

    def add(self, *paths: str) -> None:
        """Pin paths."""
        self._paths.update(paths)

    def drop(self, *paths: str) -> None:
        """Unpin paths."""
        self._paths.difference_update(paths)

    @property
    def paths(self) -> list[str]:
        """Pinned paths, sorted."""
        return sorted(self._paths)


def render_pinned(pinned: PinnedFiles, root: str | Path) -> str:
    """Return the current content of every pinned file that exists."""
    out = ""
    for rel in pinned.paths:
        path = Path(root) / rel
        if path.is_file():
            out += f"\n\n<attached: {rel}>\n{_read(path, MAX_ATTACH)}\n</attached>"
    return out
