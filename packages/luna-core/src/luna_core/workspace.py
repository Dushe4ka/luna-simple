"""Path confinement: every agent path resolves inside the project root."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from luna_core.errors import ToolError

# Windows drive-letter (``C:\...``, ``c:/...``) and UNC (``\\server\share``) paths are
# never absolute on POSIX, so they must be rejected before the absolute/relative
# branching below — otherwise they fall through as a single relative path segment.
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_RE = re.compile(r"^\\\\")

IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".luna",
        "dist",
        "build",
        ".tox",
    }
)


class OutsideWorkspace(ToolError):
    """The agent addressed a path outside the project."""


class InvalidPath(ToolError):
    """The agent's path cannot exist (NUL byte or rejected by the OS)."""


class Workspace:
    """The project root and the rules for resolving agent paths into it."""

    def __init__(self, root: str | os.PathLike) -> None:
        self.root = Path(root).resolve()

    def resolve(self, path: str) -> Path:
        """Return the absolute path for an agent path, or raise :class:`OutsideWorkspace`.

        A path the OS cannot represent (e.g. one with a NUL byte) raises
        :class:`InvalidPath`, also a :class:`ToolError`.
        """
        try:
            return self._resolve(path)
        except (ValueError, OSError) as exc:
            raise InvalidPath(f"invalid path: {str(path)!r} ({exc})") from exc

    def _resolve(self, path: str) -> Path:
        raw = str(path or "").strip()
        if "\x00" in raw:
            raise ValueError("embedded null byte")
        if _DRIVE_LETTER_RE.match(raw) or _UNC_RE.match(raw):
            raise OutsideWorkspace(f"outside the project: {raw} (project root is {self.root})")
        if raw in ("", ".", "/"):
            return self.root
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            if not self._under(candidate.resolve()):
                parts = PurePosixPath(raw.replace("\\", "/")).parts
                first = parts[1] if len(parts) > 1 else ""
                on_host = bool(first) and Path("/", first).exists()
                in_project = bool(first) and (self.root / first).exists()
                if on_host and not in_project:
                    raise OutsideWorkspace(
                        f"outside the project: {raw} (project root is {self.root})"
                    )
                candidate = self.root.joinpath(*parts[1:])
        else:
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if not self._under(resolved):
            raise OutsideWorkspace(f"outside the project: {raw} (project root is {self.root})")
        return resolved

    def rel(self, path: str | Path) -> str:
        """Project-relative POSIX form of an absolute path inside the root."""
        p = Path(path)
        if not p.is_absolute():
            p = self.resolve(str(path))
        rel = p.resolve().relative_to(self.root).as_posix()
        return rel or "."

    @staticmethod
    def ignored(rel: str) -> bool:
        """Return True for paths inside VCS, dependency, cache or Luna state directories."""
        return any(part in IGNORED_DIRS for part in PurePosixPath(rel).parts)

    def _under(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True
