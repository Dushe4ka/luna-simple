"""Per-turn file snapshots for /undo and /redo (git shadow trees or a file journal)."""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Luna state that is never part of a snapshot (subagent worktrees live under .luna too).
_EXCLUDES = (":(exclude).luna/undo", ":(exclude).luna/worktrees")


@dataclass(frozen=True)
class UndoResult:
    """What an undo or redo did."""

    turn_id: str
    note: str


def _git(root: Path, args: list[str], env: dict | None = None) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _is_git(root: Path) -> bool:
    return _git(root, ["rev-parse", "--is-inside-work-tree"]) == "true"


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

    # -- storage ---------------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads((self.dir / "journal.json").read_text())
            return data if isinstance(data, dict) else {"turns": [], "redo": []}
        except (OSError, ValueError):
            return {"turns": [], "redo": []}

    def _save(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "journal.json.tmp"
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

    # -- recording -------------------------------------------------------
    def begin_turn(self, turn_id: str) -> None:
        """Record the state before a turn; clears the redo stack."""
        data = self._load()
        record: dict = {"turn_id": turn_id, "ts": time.time()}
        if self.is_git:
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

    def before_write(self, turn_id: str, rel: str) -> None:
        """Keep ``rel``'s pre-image for this turn (file-journal mode only)."""
        if self.is_git:
            return
        data = self._load()
        turn = next((t for t in reversed(data["turns"]) if t["turn_id"] == turn_id), None)
        if turn is None or rel in turn.setdefault("files", {}):
            return
        turn["files"][rel] = self._read(rel)
        self._save(data)

    def _read(self, rel: str) -> dict:
        target = self.root / rel
        if target.is_dir():
            return {"kind": "dir"}
        if not target.is_file():
            return {"kind": "absent"}
        try:
            return {"kind": "text", "text": target.read_text(encoding="utf-8")}
        except (OSError, ValueError):
            return {"kind": "binary"}

    def _write(self, rel: str, image: dict) -> bool:
        target = self.root / rel
        kind = image.get("kind")
        if kind == "text":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(image["text"], encoding="utf-8")
            return True
        if kind == "absent":
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
            return True
        return False  # binary or directory pre-images cannot be restored

    # -- undo / redo -----------------------------------------------------
    def undo(self) -> UndoResult | None:
        """Revert the newest turn's files."""
        data = self._load()
        if not data["turns"]:
            return None
        turn = data["turns"].pop()
        if self.is_git:
            post = _snapshot_tree(self.root)
            pre = turn.get("pre_tree")
            changed = bool(pre and post and pre != post)
            if changed:
                self._restore_tree(pre, post)
            data["redo"].append({"turn_id": turn["turn_id"], "pre_tree": pre, "post_tree": post})
        else:
            files = turn.get("files", {})
            post_images = {rel: self._read(rel) for rel in files}
            skipped = [rel for rel, image in files.items() if not self._write(rel, image)]
            changed = bool(files)
            data["redo"].append(
                {
                    "turn_id": turn["turn_id"],
                    "files": files,
                    "post": post_images,
                    "skipped": skipped,
                }
            )
        self._save(data)
        return UndoResult(
            turn["turn_id"], "files restored" if changed else "no file changes in this turn"
        )

    def redo(self) -> UndoResult | None:
        """Re-apply the most recently undone turn's files."""
        data = self._load()
        if not data["redo"]:
            return None
        record = data["redo"].pop()
        if self.is_git:
            if record.get("post_tree") and record.get("pre_tree"):
                self._restore_tree(record["post_tree"], record["pre_tree"])
            data["turns"].append(
                {
                    "turn_id": record["turn_id"],
                    "pre_tree": record.get("pre_tree"),
                    "ts": time.time(),
                }
            )
        else:
            for rel, image in record.get("post", {}).items():
                self._write(rel, image)
            data["turns"].append(
                {"turn_id": record["turn_id"], "files": record.get("files", {}), "ts": time.time()}
            )
        self._save(data)
        return UndoResult(record["turn_id"], "files re-applied")

    def _restore_tree(self, target: str, current: str) -> None:
        _git(self.root, ["restore", "--source", target, "--worktree", "--", ".", *_EXCLUDES])
        # restore never deletes files that exist only in ``current``.
        added = (
            _git(
                self.root,
                ["diff", "--name-only", "--diff-filter=A", target, current, "--", ".", *_EXCLUDES],
            )
            or ""
        )
        for rel in added.splitlines():
            path = self.root / rel
            if path.is_file():
                with contextlib.suppress(OSError):
                    path.unlink()

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
            if image.get("kind") not in ("text", "absent"):
                chunks.append(f"# {rel}: binary or unreadable — changed, no diff")
                continue
            now = self._read(rel)
            before = image.get("text", "").splitlines()
            after = now.get("text", "").splitlines()
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
