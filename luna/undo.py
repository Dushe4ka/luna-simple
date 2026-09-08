"""Per-session file snapshots so ``/undo`` and ``/diff`` work without git."""

from __future__ import annotations

import contextlib
import difflib
import json
import time
from pathlib import Path


def journal_dir(workdir: str, session_id: str) -> Path:
    """Return (creating) ``<workdir>/.luna/undo/<session_id>/``."""
    path = Path(workdir) / ".luna" / "undo" / (session_id or "default")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _entries(workdir: str, session_id: str) -> list[Path]:
    try:
        return sorted(journal_dir(workdir, session_id).glob("[0-9]*.json"))
    except OSError:
        return []


def snapshot(workdir: str, session_id: str, tool: str, rel_path: str) -> None:
    """Record the pre-image of ``rel_path`` as the next ``NNNN.json`` entry."""
    d = journal_dir(workdir, session_id)
    target = Path(workdir) / rel_path
    before = target.read_text() if target.is_file() else None
    n = len(_entries(workdir, session_id))
    (d / f"{n:04d}.json").write_text(
        json.dumps({"tool": tool, "path": rel_path, "before": before, "ts": time.time()})
    )


def session_diff(workdir: str, session_id: str) -> str:
    """Unified diff from each path's earliest pre-image to its current state."""
    earliest: dict[str, str | None] = {}
    for entry in _entries(workdir, session_id):
        try:
            rec = json.loads(entry.read_text())
        except (OSError, ValueError):
            continue
        earliest.setdefault(rec["path"], rec["before"])
    chunks: list[str] = []
    for rel, before in earliest.items():
        target = Path(workdir) / rel
        current = target.read_text() if target.is_file() else ""
        diff = difflib.unified_diff(
            (before or "").splitlines(),
            current.splitlines(),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="",
        )
        text = "\n".join(diff)
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


def undo_last(workdir: str, session_id: str) -> str | None:
    """Restore (or delete) the file behind the newest journal entry."""
    entries = _entries(workdir, session_id)
    if not entries:
        return None
    try:
        rec = json.loads(entries[-1].read_text())
    except (OSError, ValueError):
        return None
    target = Path(workdir) / rec["path"]
    if rec["before"] is None:
        if target.is_file():
            with contextlib.suppress(OSError):
                target.unlink()
        note = f"removed {rec['path']}"
    else:
        with contextlib.suppress(OSError):
            target.write_text(rec["before"])
        note = f"reverted {rec['path']}"
    with contextlib.suppress(OSError):
        entries[-1].unlink()
    return note
