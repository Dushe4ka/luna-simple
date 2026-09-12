"""Per-session file snapshots so ``/undo`` and ``/diff`` work without git.

``undo()``/``redo()`` accept an already-built agent and lazily import message
classes only inside those two functions — every other function in this file
is framework-free.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from luna import gitinfo


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
    """Unified diff for this session: git tree diff, or the file-journal fallback."""
    if gitinfo.is_git_repo(workdir):
        d = _git_turns_dir(workdir, session_id)
        turns = _read_json_list(d / "turns.json")
        if not turns:
            return ""
        earliest_sha = turns[0]["pre_sha"]
        out = _run_git(workdir, ["diff", earliest_sha, "--", ".", ":(exclude).luna/undo"])
        return out or ""
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
        _run_git(workdir, ["update-ref", "-d", f"refs/luna/undo/{d.name}"])


def _run_git(workdir: str, args: list[str], env: dict | None = None) -> str | None:
    """Run a git plumbing command; return stdout on success, ``None`` on failure."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=workdir, capture_output=True, text=True, timeout=10, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _add_all_excluding_undo_journal(workdir: str, env: dict) -> bool:
    """Stage everything except ``.luna/undo`` into the temp index; report success.

    ``git add -A -- . ":(exclude).luna/undo"`` exits 1 with an "ignored paths"
    warning whenever ``.luna`` itself is gitignored (the documented default —
    ``/init`` adds it to the project's ``.gitignore``): git validates an
    ``:(exclude)`` pathspec segment against ``.gitignore`` even though it names a
    path to leave OUT, not one to add. The temp index is still built correctly
    despite that exit code — verified by inspecting the resulting tree directly.
    A genuine I/O/permission failure is a different story (exit 128, "fatal:
    adding files failed"): git never finishes writing the index in that case, and
    treating it as fine would let ``write-tree`` silently produce an EMPTY tree —
    which ``undo()`` would then read as "every file in the worktree was added by
    this turn" and delete all of them. Exit 1 alongside exit 0 is accepted here;
    anything else is treated as a real failure — but only because ``add.ignoreErrors``
    is forced off: with it set to true (a real, if unusual, user/global git config),
    a genuine indexing failure ALSO exits 1 (rather than 128) while still leaving
    the failed path out of the index, which would otherwise slip past this exact
    check and cause the same silent-deletion outcome this function exists to
    prevent. Pinning it here makes the exit code meaningful regardless of the
    caller's git config.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-c",
                "add.ignoreErrors=false",
                "add",
                "-A",
                "--",
                ".",
                ":(exclude).luna/undo",
            ],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode in (0, 1)


def _snapshot_tree(workdir: str) -> str | None:
    """Write the current worktree to a tree object without touching the real index."""
    with tempfile.TemporaryDirectory() as tmp:
        index_file = str(Path(tmp) / "index")
        env = {**os.environ, "GIT_INDEX_FILE": index_file}
        if not _add_all_excluding_undo_journal(workdir, env):
            return None
        return _run_git(workdir, ["write-tree"], env=env)


def _git_turns_dir(workdir: str, session_id: str) -> Path:
    path = Path(workdir) / ".luna" / "undo" / (session_id or "default")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json_list(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_json_list(path: Path, data: list[dict]) -> None:
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(data))


def begin_turn(workdir: str, session_id: str, message_count: int) -> None:
    """Snapshot the worktree into a shadow ref before a turn (git repos only).

    No-op outside a git repository. Never raises.
    """
    if not gitinfo.is_git_repo(workdir):
        return
    tree = _snapshot_tree(workdir)
    if tree is None:
        return
    parent = _run_git(workdir, ["rev-parse", "HEAD"])
    commit_args = ["commit-tree", tree, "-m", "luna turn snapshot"]
    if parent:
        commit_args += ["-p", parent]
    sha = _run_git(workdir, commit_args)
    if not sha:
        return
    _run_git(workdir, ["update-ref", f"refs/luna/undo/{session_id or 'default'}", sha])
    d = _git_turns_dir(workdir, session_id)
    turns = _read_json_list(d / "turns.json")
    turns.append({"turn": len(turns), "pre_sha": sha, "message_count": message_count})
    _write_json_list(d / "turns.json", turns)
    _write_json_list(d / "redo.json", [])  # a new turn clears any pending redo


def forget_messages(workdir: str, session_id: str, message_count: int) -> None:
    """Resync the git-path ledger after the conversation itself was rewritten.

    (e.g. by ``/compact``), so a later ``undo()`` doesn't try to remove messages
    that no longer exist. Files stay revertible via each turn's own ``pre_sha``;
    every EXISTING turn's message-removal step becomes a no-op — there is no way
    to map its stale, pre-compact ``message_count`` onto the new, collapsed
    numbering, so every recorded turn (not just the most recent) is reset to the
    current count — until a fresh turn begins and records its own accurate count.
    No-op outside a git repository or when there's no turn on record. Never
    raises.
    """
    if not gitinfo.is_git_repo(workdir):
        return
    d = _git_turns_dir(workdir, session_id)
    turns = _read_json_list(d / "turns.json")
    if turns:
        for t in turns:
            t["message_count"] = message_count
        _write_json_list(d / "turns.json", turns)
    _write_json_list(d / "redo.json", [])


def _message_to_dict(msg) -> dict:
    """Serialise one BaseMessage well enough to rebuild it with the same id."""
    return {
        "id": getattr(msg, "id", None),
        "type": getattr(msg, "type", "human"),
        "content": getattr(msg, "content", ""),
        "tool_calls": getattr(msg, "tool_calls", None),
        "tool_call_id": getattr(msg, "tool_call_id", None),
        "name": getattr(msg, "name", None),
    }


def _dict_to_message(data: dict):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    msg_id = data.get("id") or uuid4().hex
    kind = data.get("type")
    if kind == "tool":
        return ToolMessage(
            id=msg_id,
            content=data.get("content", ""),
            tool_call_id=data.get("tool_call_id") or "",
            name=data.get("name"),
        )
    if kind == "ai":
        kwargs = {"id": msg_id, "content": data.get("content", "")}
        if data.get("tool_calls"):
            kwargs["tool_calls"] = data["tool_calls"]
        return AIMessage(**kwargs)
    return HumanMessage(id=msg_id, content=data.get("content", ""))


def _added_paths(workdir: str, old: str, new: str) -> list[str]:
    """Paths present in ``new`` but not in ``old`` (a checkout from new->old won't delete them)."""
    out = _run_git(
        workdir,
        ["diff", "--name-only", "--diff-filter=A", old, new, "--", ".", ":(exclude).luna/undo"],
    )
    return [p for p in (out or "").splitlines() if p.strip()]


def _unlink_stragglers(workdir: str, paths: list[str]) -> None:
    for p in paths:
        target = Path(workdir) / p
        if target.is_file():
            with contextlib.suppress(OSError):
                target.unlink()


def undo(workdir: str, session_id: str, agent, thread_id: str) -> str | None:
    """Revert the last turn's files and truncate the conversation (git path)."""
    d = _git_turns_dir(workdir, session_id)
    turns = _read_json_list(d / "turns.json")
    if not turns:
        return None
    record = turns[-1]

    config = {"configurable": {"thread_id": thread_id}}
    post_sha = _snapshot_tree(workdir)
    try:
        state_messages = agent.get_state(config).values.get("messages", [])
    except Exception:  # noqa: BLE001 - a stub/broken agent must not corrupt the ledger
        state_messages = []
    added = state_messages[record["message_count"] :]

    _run_git(
        workdir,
        ["restore", "--source", record["pre_sha"], "--worktree", "--", ".", ":(exclude).luna/undo"],
    )
    if post_sha:
        # A restore only rewrites content for paths present in pre_sha's tree —
        # it never deletes a file the turn created, which has no counterpart there.
        _unlink_stragglers(workdir, _added_paths(workdir, record["pre_sha"], post_sha))

    from langchain_core.messages import RemoveMessage

    removable = [m for m in added if getattr(m, "id", None)]
    if removable:
        agent.update_state(config, {"messages": [RemoveMessage(id=m.id) for m in removable]})

    turns.pop()
    _write_json_list(d / "turns.json", turns)

    redo_stack = _read_json_list(d / "redo.json")
    redo_stack.append(
        {
            "post_sha": post_sha,
            "pre_sha": record["pre_sha"],  # retained for a future redo -> undo
            "message_count": record["message_count"],
            "messages": [_message_to_dict(m) for m in added],
        }
    )
    _write_json_list(d / "redo.json", redo_stack)
    note = f"undid turn {record['turn']} — {len(added)} message(s), files restored"
    if len(removable) != len(added):
        note += f" ({len(added) - len(removable)} message(s) could not be removed)"
    return note


def redo(workdir: str, session_id: str, agent, thread_id: str) -> str | None:
    """Re-apply the most recently undone turn's files and messages (git path)."""
    d = _git_turns_dir(workdir, session_id)
    redo_stack = _read_json_list(d / "redo.json")
    if not redo_stack:
        return None
    record = redo_stack[-1]

    if record.get("post_sha"):
        _run_git(
            workdir,
            [
                "restore",
                "--source",
                record["post_sha"],
                "--worktree",
                "--",
                ".",
                ":(exclude).luna/undo",
            ],
        )
        if record.get("pre_sha"):
            # The mirror case: a file the turn deleted is still sitting on disk
            # from pre_sha's state — a restore to post_sha won't remove it.
            _unlink_stragglers(
                workdir, _added_paths(workdir, record["post_sha"], record["pre_sha"])
            )
    config = {"configurable": {"thread_id": thread_id}}
    rebuilt = [_dict_to_message(m) for m in record.get("messages", [])]
    if rebuilt:
        agent.update_state(config, {"messages": rebuilt})

    redo_stack.pop()
    _write_json_list(d / "redo.json", redo_stack)

    turns = _read_json_list(d / "turns.json")
    turns.append(
        {
            "turn": len(turns),
            "pre_sha": record.get("pre_sha", ""),
            "message_count": record["message_count"],
        }
    )
    _write_json_list(d / "turns.json", turns)
    return "redo applied"
