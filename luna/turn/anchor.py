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
from pathlib import Path


def hash_file(workdir: str, rel_path: str) -> str | None:
    """Return the sha256 hex digest of the file's current bytes, or None if it can't be read."""
    try:
        return hashlib.sha256((Path(workdir) / rel_path).read_bytes()).hexdigest()
    except OSError:
        return None


class AnchorTracker:
    """Per-session record of the last-seen content hash for tracked paths."""

    def __init__(self) -> None:
        self._anchors: dict[str, str] = {}

    def remember(self, workdir: str, rel_path: str) -> None:
        """Record the file's current content hash as the new anchor."""
        digest = hash_file(workdir, rel_path)
        if digest is not None:
            self._anchors[rel_path] = digest

    def check(self, workdir: str, rel_path: str) -> bool:
        """Return True if there's no anchor yet, or the anchor still matches disk."""
        anchored = self._anchors.get(rel_path)
        if anchored is None:
            return True
        return hash_file(workdir, rel_path) == anchored

    def forget(self, rel_path: str) -> None:
        """Drop the anchor for a deleted path."""
        self._anchors.pop(rel_path, None)
