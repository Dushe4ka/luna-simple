"""Per-turn file snapshots for /undo and /redo (git shadow trees or a file journal)."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Luna state that is never part of a snapshot (subagent worktrees live under .luna too).
_EXCLUDES = (":(exclude).luna/undo", ":(exclude).luna/worktrees")


# One lock per journal directory: parallel tool calls (threads) and several
# ``UndoJournal`` objects for the same session must not lose each other's updates.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(directory: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(directory), threading.RLock())


@dataclass(frozen=True)
class UndoResult:
    """What an undo or redo did."""

    turn_id: str
    note: str


def _git(root: Path, args: list[str], env: dict | None = None, *, raw: bool = False) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout if raw else proc.stdout.strip()


def _changed(root: Path, a: str, b: str) -> dict[str, str] | None:
    """``{path: status}`` of the paths that differ between trees ``a`` and ``b``.

    ``A`` = only in ``b``, ``D`` = only in ``a``, anything else = in both.
    """
    out = _git(
        root,
        ["diff", "--name-status", "--no-renames", "-z", a, b, "--", ".", *_EXCLUDES],
        raw=True,
    )
    if out is None:
        return None
    parts = out.split("\0")
    return {parts[i + 1]: parts[i][:1] for i in range(0, len(parts) - 1, 2) if parts[i]}


def _in_scope(path: str, scope: list[str]) -> bool:
    """Whether ``path`` is one of ``scope`` or lies below one of them (a deleted folder)."""
    return any(path == s or path.startswith(s.rstrip("/") + "/") for s in scope)


def _fingerprint(image: dict) -> str:
    """Compact identity of a pre/post-image (content hash for files)."""
    if image.get("kind") == "bytes":
        return "sha256:" + hashlib.sha256(base64.b64decode(image["data"])).hexdigest()
    return str(image.get("kind"))


def _note(verb: str, applied: int, skipped: list[str], failed: list[str], since: str) -> str:
    """Human summary of an undo/redo; names every path it did not touch or could not."""
    if not skipped and not failed:
        return f"files {verb}"
    parts = [f"{verb} {applied} files"]
    if skipped:
        parts.append(f"skipped {', '.join(sorted(skipped))}: changed since the {since}")
    if failed:
        parts.append(f"could not restore: {', '.join(sorted(failed))}")
    return "; ".join(parts)


def _is_git(root: Path) -> bool:
    return _git(root, ["rev-parse", "--is-inside-work-tree"]) == "true"


def _decode(image: dict) -> str:
    """Best-effort text view of a ``"bytes"`` pre/post-image, for ``diff()`` display."""
    if image.get("kind") != "bytes":
        return ""
    return base64.b64decode(image["data"]).decode("utf-8", errors="replace")


def _snapshot_tree(root: Path) -> str | None:
    """Write the worktree to a tree object without touching the real index.

    ``git add`` exits 1 with an "ignored paths" warning when ``.luna`` is
    gitignored, although the temp index is built correctly; exit 128 means a
    real failure and must not produce an empty tree (an undo would then delete
    every file). ``add.ignoreErrors`` is pinned off so exit 1 cannot hide a
    genuine indexing error.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        try:
            proc = subprocess.run(
                ["git", "-c", "add.ignoreErrors=false", "add", "-A", "--", ".", *_EXCLUDES],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode not in (0, 1):
            return None
        return _git(root, ["write-tree"], env=env)


class UndoJournal:
    """File history of one session, one entry per turn."""

    def __init__(self, root: str | Path, session_id: str) -> None:
        self.root = Path(root).resolve()
        self.session_id = session_id or "default"
        self.dir = self.root / ".luna" / "undo" / self.session_id
        self.is_git = _is_git(self.root)
        self._lock = _lock_for(self.dir)

    # -- storage ---------------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads((self.dir / "journal.json").read_text())
            return data if isinstance(data, dict) else {"turns": [], "redo": []}
        except (OSError, ValueError):
            return {"turns": [], "redo": []}

    def _save(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f"journal.json.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(self.dir / "journal.json")

    @property
    def depth(self) -> int:
        """Number of turns that can be undone."""
        return len(self._load()["turns"])

    @property
    def redo_depth(self) -> int:
        """Number of undone turns that can be redone."""
        return len(self._load()["redo"])

    def top_turn(self) -> str | None:
        """Turn id of the newest undoable record, or ``None``."""
        turns = self._load()["turns"]
        return turns[-1]["turn_id"] if turns else None

    def discard_top(self) -> None:
        """Drop the newest undoable record without touching any files.

        Used when a record has no matching visible conversation turn (a
        turn whose ``begin_turn`` ran but was cancelled before any message
        was logged) — there is nothing for the user to see restored, so the
        record is dropped in place rather than undone.
        """
        with self._lock:
            data = self._load()
            if data["turns"]:
                data["turns"].pop()
                self._save(data)

    def top_redo(self) -> str | None:
        """Turn id of the newest redoable record, or ``None``."""
        redo = self._load()["redo"]
        return redo[-1]["turn_id"] if redo else None

    # -- recording -------------------------------------------------------
    def begin_turn(self, turn_id: str) -> None:
        """Record the state before a turn; clears the redo stack."""
        with self._lock:
            data = self._load()
            record: dict = {"turn_id": turn_id, "ts": time.time()}
            if self.is_git:
                # paths touched by file tools; ``commands``: a shell command ran
                record.update(paths=[], commands=False)
                tree = _snapshot_tree(self.root)
                if tree:
                    record["pre_tree"] = tree
                    commit = _git(self.root, ["commit-tree", tree, "-m", f"luna turn {turn_id}"])
                    if commit:
                        _git(self.root, ["update-ref", f"refs/luna/undo/{self.session_id}", commit])
            else:
                record["files"] = {}
            data["turns"].append(record)
            data["redo"] = []
            self._save(data)

    def end_turn(self, turn_id: str) -> None:
        """Record what ``turn_id`` left behind, so undo can tell later user edits apart.

        Git mode keeps the end tree; journal mode a fingerprint of every
        journaled file. Undo/redo then touch only the paths the turn changed
        and skip any whose content moved on since (edited by the user).
        """
        with self._lock:
            data = self._load()
            turn = next((t for t in reversed(data["turns"]) if t["turn_id"] == turn_id), None)
            if turn is None:
                return
            if self.is_git:
                tree = _snapshot_tree(self.root)
                if not tree:
                    return
                turn["end_tree"] = tree
            else:
                turn["end"] = {rel: _fingerprint(self._read(rel)) for rel in turn.get("files", {})}
            self._save(data)

    def before_write(self, turn_id: str, rel: str) -> None:
        """Note that a file tool is about to change ``rel`` in this turn.

        Git mode remembers the path (undo restores only such paths); the
        file journal keeps ``rel``'s pre-image.
        """
        with self._lock:
            data = self._load()
            turn = next((t for t in reversed(data["turns"]) if t["turn_id"] == turn_id), None)
            if turn is None:
                return
            if self.is_git:
                paths = turn.setdefault("paths", [])
                if rel in paths:
                    return
                paths.append(rel)
            else:
                if rel in turn.setdefault("files", {}):
                    return
                turn["files"][rel] = self._read(rel)
            self._save(data)

    def note_command(self, turn_id: str) -> None:
        """Note that a shell command (which may touch any file) ran in this turn."""
        with self._lock:
            data = self._load()
            turn = next((t for t in reversed(data["turns"]) if t["turn_id"] == turn_id), None)
            if turn is None or turn.get("commands"):
                return
            turn["commands"] = True
            self._save(data)

    def _read(self, rel: str) -> dict:
        """Capture ``rel``'s current content as raw bytes (base64 in the journal).

        Storing bytes rather than decoded text means a pre-image round-trips
        exactly — CRLF line endings and non-UTF-8 files are restored
        byte-for-byte instead of being normalised or dropped.
        """
        target = self.root / rel
        if target.is_dir():
            return {"kind": "dir"}
        if not target.is_file():
            return {"kind": "absent"}
        try:
            data = target.read_bytes()
        except OSError:
            return {"kind": "absent"}
        return {"kind": "bytes", "data": base64.b64encode(data).decode("ascii")}

    def _write(self, rel: str, image: dict) -> bool:
        target = self.root / rel
        kind = image.get("kind")
        if kind == "bytes":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(image["data"]))
            return True
        if kind == "absent":
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
            return True
        return False  # a directory pre-image cannot be restored (no captured contents)

    # -- undo / redo -----------------------------------------------------
    def undo(self) -> UndoResult | None:
        """Revert the newest turn's files.

        Only paths the turn itself changed are touched, and a path whose
        content changed after the turn ended (a user edit) is skipped and
        named in the note. The turn record is popped (and a redo record
        pushed) even when files could not be restored — the note says so
        instead of claiming "no file changes". The journal is saved even
        when a restore fails half-way.
        """
        with self._lock:
            data = self._load()
            if not data["turns"]:
                return None
            turn = data["turns"].pop()
            try:
                if self.is_git:
                    note, record = self._undo_git(turn)
                else:
                    note, record = self._undo_files(turn)
                data["redo"].append(record)
            finally:
                self._save(data)
            return UndoResult(turn["turn_id"], note)

    def _undo_git(self, turn: dict) -> tuple[str, dict]:
        """Undo in git mode.

        Only paths touched by file tools are restored — the end tree is a
        whole-tree snapshot and also holds what the user edited meanwhile.
        A turn that ran shell commands restores its whole diff (commands
        can change any file) and names every path beyond the file tools'.
        Without a recorded end (process died mid-turn) "now" stands in for
        it, and then only file-tool paths are restored.
        """
        pre = turn.get("pre_tree")
        paths = turn.get("paths")  # None: a journal from before paths were recorded
        end = turn.get("end_tree")
        commands = bool(turn.get("commands")) and end is not None
        if end is None:
            end = _snapshot_tree(self.root)
        scope = None if commands or paths is None else paths
        record = {
            "turn_id": turn["turn_id"],
            "pre_tree": pre,
            "post_tree": end,
            "scope": scope,
            "paths": paths,
            "commands": bool(turn.get("commands")),
        }
        if not pre or not end:
            return "files could not be restored (no snapshot of the turn)", record
        result = self._apply_tree(end, pre, scope)
        if result is None:
            return "no file changes in this turn", record
        done, skipped, failed = result
        note = _note("restored", len(done), skipped, failed, "turn")
        if commands and paths is not None:
            extra = sorted(p for p in done if not _in_scope(p, paths))
            if extra:
                note += f"; also restored (changed by commands or outside Luna): {', '.join(extra)}"
        return note, record

    def _undo_files(self, turn: dict) -> tuple[str, dict]:
        files = turn.get("files", {})
        end = turn.get("end")
        post: dict[str, dict] = {}
        skipped: list[str] = []
        failed: list[str] = []
        for rel, image in files.items():
            now = self._read(rel)
            if end is not None and _fingerprint(now) != end.get(rel):
                if _fingerprint(now) != _fingerprint(image):
                    skipped.append(rel)
                continue
            try:
                ok = self._write(rel, image)
            except OSError:
                ok = False
            if ok:
                post[rel] = now
            else:
                failed.append(rel)
        record = {
            "turn_id": turn["turn_id"],
            "files": files,
            "post": post,
            "skipped": skipped + failed,
        }
        if not files:
            return "no file changes in this turn", record
        return _note("restored", len(post), skipped, failed, "turn"), record

    def redo(self) -> UndoResult | None:
        """Re-apply the most recently undone turn's files.

        A path whose content is no longer what the undo left (edited since)
        is skipped and named in the note.
        """
        with self._lock:
            data = self._load()
            if not data["redo"]:
                return None
            record = data["redo"].pop()
            note = "files re-applied"
            try:
                if self.is_git:
                    pre, end = record.get("pre_tree"), record.get("post_tree")
                    result = (
                        self._apply_tree(pre, end, record.get("scope")) if pre and end else None
                    )
                    if result is not None:
                        done, skipped, failed = result
                        note = _note("re-applied", len(done), skipped, failed, "undo")
                    turn = {"turn_id": record["turn_id"], "pre_tree": pre, "ts": time.time()}
                    if end:
                        turn["end_tree"] = end
                    if record.get("paths") is not None:
                        turn["paths"] = record["paths"]
                    turn["commands"] = record.get("commands", False)
                    data["turns"].append(turn)
                else:
                    note, turn = self._redo_files(record)
                    data["turns"].append(turn)
            finally:
                self._save(data)
            return UndoResult(record["turn_id"], note)

    def _redo_files(self, record: dict) -> tuple[str, dict]:
        files = record.get("files", {})
        # Paths not re-applied stay at their pre-image: a later undo leaves them alone.
        end = {rel: _fingerprint(image) for rel, image in files.items()}
        skipped: list[str] = []
        failed: list[str] = []
        applied = 0
        for rel, image in record.get("post", {}).items():
            now = self._read(rel)
            if _fingerprint(now) != _fingerprint(files.get(rel, {})):
                if _fingerprint(now) != _fingerprint(image):
                    skipped.append(rel)
                continue
            try:
                ok = self._write(rel, image)
            except OSError:
                ok = False
            if ok:
                applied += 1
                end[rel] = _fingerprint(image)
            else:
                failed.append(rel)
        turn = {"turn_id": record["turn_id"], "files": files, "end": end, "ts": time.time()}
        return _note("re-applied", applied, skipped, failed, "undo"), turn

    def _apply_tree(
        self, source: str, target: str, scope: list[str] | None = None
    ) -> tuple[list[str], list[str], list[str]] | None:
        """Move the paths that differ between ``source`` and ``target`` to ``target``.

        Only paths inside ``scope`` (``None``: all) are considered. A path is
        only touched while the worktree still holds its ``source`` content;
        one already at ``target`` is left as is, any other is skipped
        (changed by someone else). Returns ``(applied paths, skipped,
        failed)``, or ``None`` when no path in scope differs.
        """
        unavailable: tuple[list[str], list[str], list[str]] = (
            [],
            [],
            ["(no snapshot of the worktree)"],
        )
        paths = _changed(self.root, source, target)
        if paths is None:
            return unavailable
        if scope is not None:
            paths = {p: st for p, st in paths.items() if _in_scope(p, scope)}
        if not paths:
            return None
        current = _snapshot_tree(self.root)
        if current is None:
            return unavailable
        moved = _changed(self.root, source, current)
        pending = _changed(self.root, current, target)
        if moved is None or pending is None:
            return unavailable
        skipped = [p for p in paths if p in moved and p in pending]
        todo = [p for p in paths if p not in moved]
        restore = [p for p in todo if paths[p] != "D"]
        remove = [p for p in todo if paths[p] == "D"]
        failed: list[str] = []
        for i in range(0, len(restore), 100):
            chunk = restore[i : i + 100]
            args = ["--literal-pathspecs", "restore", "--source", target, "--worktree", "--"]
            if _git(self.root, [*args, *chunk]) is None:
                failed += chunk
        for rel in remove:
            path = self.root / rel
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
            except OSError:
                failed.append(rel)
        return [p for p in todo if p not in failed], skipped, failed

    def diff(self) -> str:
        """Unified diff of everything this session changed."""
        data = self._load()
        if not data["turns"]:
            return ""
        if self.is_git:
            first = next((t.get("pre_tree") for t in data["turns"] if t.get("pre_tree")), None)
            now = _snapshot_tree(self.root)
            return (
                (_git(self.root, ["diff", first, now, "--", ".", *_EXCLUDES]) or "")
                if first and now
                else ""
            )
        earliest: dict[str, dict] = {}
        for turn in data["turns"]:
            for rel, image in turn.get("files", {}).items():
                earliest.setdefault(rel, image)
        chunks = []
        for rel, image in earliest.items():
            if image.get("kind") not in ("bytes", "absent"):
                chunks.append(f"# {rel}: a directory — changed, no diff")
                continue
            now = self._read(rel)
            before = _decode(image).splitlines()
            after = _decode(now).splitlines()
            text = "\n".join(
                difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}", lineterm="")
            )
            if text:
                chunks.append(text)
        return "\n\n".join(chunks)


def gc(
    root: str | Path, *, keep_days: int = 7, keep_max: int = 20, keep: str | None = None
) -> None:
    """Remove old per-session journals (and their shadow refs)."""
    base = Path(root) / ".luna" / "undo"
    if not base.is_dir():
        return
    cutoff = time.time() - keep_days * 86400

    def mtime(d: Path) -> float:
        try:
            return (d / "journal.json").stat().st_mtime
        except OSError:
            return d.stat().st_mtime

    dated = sorted(
        ((d, mtime(d)) for d in base.iterdir() if d.is_dir() and d.name != keep), key=lambda t: t[1]
    )
    survivors = [d for d, m in dated if m >= cutoff]
    doomed = [d for d, m in dated if m < cutoff]
    if len(survivors) > keep_max:
        doomed += survivors[: len(survivors) - keep_max]
    for d in doomed:
        shutil.rmtree(d, ignore_errors=True)
        _git(Path(root), ["update-ref", "-d", f"refs/luna/undo/{d.name}"])
