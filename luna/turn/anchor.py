"""Anchor tracking for staleness-checked file edits (roadmap item #2).

``AnchorTracker`` records, per session, the content hash Luna last saw for
each path it has read or written. Before a mutating call on a path with
an existing anchor, ``luna/core/toolguard.py`` checks whether the file
still matches — catching edits based on stale context (a hand edit by the
user, another ``luna`` process, or simply model context that outlived the
file's actual state) with one mechanism, regardless of what changed the
file. A path with no anchor yet is never blocked: there's nothing to be
stale against.
"""

from __future__ import annotations

import hashlib
import posixpath
from pathlib import Path


def hash_file(workdir: str, rel_path: str) -> str | None:
    """Return the sha256 hex digest of the file's current bytes, or None if it can't be read."""
    try:
        return hashlib.sha256((Path(workdir) / rel_path).read_bytes()).hexdigest()
    except OSError:
        return None


def _key(rel_path: str) -> str:
    """Normalize a relative path so differently-spelled aliases share one anchor."""
    return posixpath.normpath(rel_path.lstrip("/"))


class AnchorTracker:
    """Per-session record of the last-seen content hash for tracked paths."""

    def __init__(self) -> None:
        self._anchors: dict[str, str] = {}

    def remember(self, workdir: str, rel_path: str) -> None:
        """Record the file's current content hash as the new anchor."""
        key = _key(rel_path)
        digest = hash_file(workdir, key)
        if digest is not None:
            self._anchors[key] = digest

    def check(self, workdir: str, rel_path: str) -> bool:
        """Return True if there's no anchor yet, or the anchor still matches disk."""
        key = _key(rel_path)
        anchored = self._anchors.get(key)
        if anchored is None:
            return True
        return hash_file(workdir, key) == anchored

    def forget(self, rel_path: str) -> None:
        """Drop the anchor for a deleted or unreadable path."""
        self._anchors.pop(_key(rel_path), None)

    def forget_under(self, rel_path: str) -> None:
        """Drop the anchor for rel_path and for every anchor nested under it."""
        prefix = _key(rel_path)
        self._anchors.pop(prefix, None)
        for key in [k for k in self._anchors if k.startswith(prefix + "/")]:
            del self._anchors[key]
