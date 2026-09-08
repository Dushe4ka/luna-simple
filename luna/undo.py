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
    if target.is_file():
        try:
            before = target.read_text()
        except (OSError, ValueError):  # unreadable or non-UTF-8 (e.g. binary)
            before = None
    else:
        before = None
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
        rel = rec.get("path")
        if rel is None:
            continue
        earliest.setdefault(rel, rec.get("before"))
    chunks: list[str] = []
    for rel, before in earliest.items():
        target = Path(workdir) / rel
        if target.is_file():
            try:
                current = target.read_text()
            except (OSError, ValueError):
                current = ""
        else:
            current = ""
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


def peek_last(workdir: str, session_id: str) -> str | None:
    """Describe what :func:`undo_last` would revert, without mutating anything.

    Returns ``"revert <path>"`` (or ``"delete <path> (was newly created)"``
    when the newest entry recorded no pre-image), or ``None`` if the journal
    is empty or unreadable.
    """
    entries = _entries(workdir, session_id)
    if not entries:
        return None
    try:
        rec = json.loads(entries[-1].read_text())
    except (OSError, ValueError):
        return None
    rel = rec.get("path")
    if rel is None:
        return None
    if rec.get("before") is None:
        return f"delete {rel} (was newly created)"
    return f"revert {rel}"


def undo_last(workdir: str, session_id: str) -> str | None:
    """Restore (or delete) the file behind the newest journal entry."""
    entries = _entries(workdir, session_id)
    if not entries:
        return None
    try:
        rec = json.loads(entries[-1].read_text())
    except (OSError, ValueError):
        return None
    rel = rec.get("path")
    if rel is None:
        with contextlib.suppress(OSError):
            entries[-1].unlink()
        return None
    target = Path(workdir) / rel
    before = rec.get("before")
    if before is None:
        if target.is_file():
            with contextlib.suppress(OSError):
                target.unlink()
        note = f"removed {rel}"
    else:
        with contextlib.suppress(OSError):
            target.write_text(before)
        note = f"reverted {rel}"
    with contextlib.suppress(OSError):
        entries[-1].unlink()
    return note
