"""Per-session file snapshots so ``/undo`` and ``/diff`` work without git."""

from __future__ import annotations

import contextlib
import difflib
import json
import shutil
import time
from pathlib import Path
from uuid import uuid4


def journal_dir(workdir: str, session_id: str) -> Path:
    """Return (creating) ``<workdir>/.luna/undo/<session_id>/``."""
    path = Path(workdir) / ".luna" / "undo" / (session_id or "default")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _journal_path(workdir: str, session_id: str) -> Path:
    """Return ``<workdir>/.luna/undo/<session_id>/`` without creating it."""
    return Path(workdir) / ".luna" / "undo" / (session_id or "default")


def _entries(workdir: str, session_id: str) -> list[Path]:
    d = _journal_path(workdir, session_id)
    if not d.is_dir():
        return []
    try:
        return sorted(d.glob("[0-9]*.json"))
    except OSError:
        return []


def _existed(rec: dict) -> bool:
    """Pre-0.2.1 entries had no 'existed' key: before=None meant 'created'."""
    if "existed" in rec:
        return bool(rec["existed"])
    return rec.get("before") is not None


def snapshot(workdir: str, session_id: str, tool: str, rel_path: str) -> None:
    """Record the pre-image of ``rel_path`` as a new journal entry.

    Entries are named ``<time_ns>-<rand>.json``. ``time.time_ns()`` is
    fixed-width for centuries, so a lexicographic sort of the entries stays
    chronological; the random suffix breaks ties between snapshots taken in the
    same nanosecond (LangGraph runs tool calls from one AI message in parallel).
    """
    d = journal_dir(workdir, session_id)
    target = Path(workdir) / rel_path
    existed = target.is_file()
    before: str | None = None
    if existed:
        try:
            before = target.read_text()
        except (OSError, ValueError):  # unreadable or non-UTF-8 (binary)
            before = None
    (d / f"{time.time_ns()}-{uuid4().hex[:8]}.json").write_text(
        json.dumps(
            {
                "tool": tool,
                "path": rel_path,
                "before": before,
                "existed": existed,
                "ts": time.time(),
            }
        )
    )


def session_diff(workdir: str, session_id: str) -> str:
    """Unified diff from each path's earliest pre-image to its current state."""
    earliest: dict[str, tuple[str | None, bool]] = {}
    for entry in _entries(workdir, session_id):
        try:
            rec = json.loads(entry.read_text())
        except (OSError, ValueError):
            continue
        rel = rec.get("path")
        if rel is None:
            continue
        earliest.setdefault(rel, (rec.get("before"), _existed(rec)))
    chunks: list[str] = []
    for rel, rec in earliest.items():
        before, existed = rec
        target = Path(workdir) / rel
        current = ""
        if target.is_file():
            try:
                current = target.read_text()
            except (OSError, ValueError):
                chunks.append(f"# {rel}: binary or unreadable — changed, no diff")
                continue
        if before is None and existed:
            chunks.append(f"# {rel}: binary or unreadable — changed, no diff")
            continue
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

    Returns ``"revert <path>"``, ``"skip <path> (binary/unreadable original)"``
    when the pre-image could not be captured for a file that existed, or
    ``"delete <path> (was newly created)"`` when the newest entry recorded no
    pre-image for a file that did not exist. Returns ``None`` if the journal is
    empty or unreadable.
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
    before, existed = rec.get("before"), _existed(rec)
    if before is not None:
        return f"revert {rel}"
    if existed:
        return f"skip {rel} (binary/unreadable original)"
    return f"delete {rel} (was newly created)"


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
    before, existed = rec.get("before"), _existed(rec)
    if before is not None:
        with contextlib.suppress(OSError):
            target.write_text(before)
        note = f"reverted {rel}"
    elif existed:
        note = f"skipped {rel}: original was binary or unreadable, cannot revert"
    else:
        if target.is_file():
            with contextlib.suppress(OSError):
                target.unlink()
        note = f"removed {rel}"
    with contextlib.suppress(OSError):
        entries[-1].unlink()
    return note


def gc(
    workdir: str,
    *,
    keep_days: int = 7,
    keep_max: int = 20,
    keep: str | None = None,
) -> None:
    """Remove stale per-session undo journals under ``<workdir>/.luna/undo/``.

    ``keep`` is a ``session_id`` whose journal directory is never removed — pass
    the id of the session being resumed so ``--continue`` can still reach an old
    journal.
    """
    root = Path(workdir) / ".luna" / "undo"
    if not root.is_dir():
        return
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return
    cutoff = time.time() - keep_days * 86400

    def _mtime(d: Path) -> float:
        entries = sorted(d.glob("[0-9]*.json"))
        try:
            return entries[-1].stat().st_mtime if entries else d.stat().st_mtime
        except OSError:
            return 0.0

    dated = sorted(((d, _mtime(d)) for d in dirs if d.name != keep), key=lambda t: t[1])
    survivors = [d for d, m in dated if m >= cutoff]
    to_remove = [d for d, m in dated if m < cutoff]
    if len(survivors) > keep_max:
        to_remove += survivors[: len(survivors) - keep_max]
    for d in to_remove:
        with contextlib.suppress(OSError):
            shutil.rmtree(d)
