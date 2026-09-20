"""wrap_tool_call middleware: enforce deny rules and snapshot mutating calls."""

from __future__ import annotations

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from luna.core.permissions import RuleSet
from luna.turn import gitinfo
from luna.turn.anchor import AnchorTracker
from luna.turn.undo import snapshot

_MUTATING = {"write_file", "edit_file", "delete"}
_TRACKED = {"read_file", "write_file", "edit_file", "delete"}


def tool_guard(rules: RuleSet, workdir: str, session_id: str = "", plan=None):
    """Build a ``wrap_tool_call`` middleware that blocks ``deny``-matched calls.

    Non-denied ``write_file`` / ``edit_file`` / ``delete`` calls are snapshotted
    into the per-session undo journal before the tool runs, unless ``workdir``
    is a git repo (then ``undo.begin_turn`` handles snapshots instead). ``plan``
    is an optional ``Callable[[], bool]``; while it returns ``True``, mutating
    calls (``write_file`` / ``edit_file`` / ``delete`` / ``execute``) are refused.
    A ``write_file`` / ``edit_file`` / ``delete`` call on a path this session
    has already read or written is refused if the file's on-disk content no
    longer matches what was last seen — see ``luna.turn.anchor``.
    """
    use_journal = session_id and not gitinfo.is_git_repo(workdir)
    tracker = AnchorTracker()

    @wrap_tool_call
    def _guard(request, handler):
        call = request.tool_call
        name = call.get("name", "")
        args = call.get("args", {}) or {}
        if rules.match(name, args) == "deny":
            return ToolMessage(
                content=f"blocked by a Luna permission rule ({name})",
                tool_call_id=call.get("id", "blocked"),
                status="error",
            )
        if plan is not None and plan() and name in {"write_file", "edit_file", "delete", "execute"}:
            return ToolMessage(
                content=f"plan mode is on — refusing to {name}. Run /plan off to make changes.",
                tool_call_id=call.get("id", "blocked"),
                status="error",
            )
        rel = (args.get("file_path") or args.get("path") or "").lstrip("/")
        if rel and name in _MUTATING and not tracker.check(workdir, rel):
            return ToolMessage(
                content=(
                    f"{rel} has changed on disk since Luna last saw it "
                    "(hand edit, another process, or Luna's own formatter) — "
                    "read it again before retrying"
                ),
                tool_call_id=call.get("id", "blocked"),
                status="error",
            )
        if use_journal and name in _MUTATING and rel:
            try:
                snapshot(workdir, session_id, name, rel)
            except (OSError, ValueError):
                pass
        result = handler(request)
        ok = getattr(result, "status", "success") != "error"
        if rel and name in _TRACKED:
            if name == "delete":
                if ok:
                    tracker.forget_under(rel)
            elif name == "read_file" and not ok:
                tracker.forget(rel)
            elif ok:
                tracker.remember(workdir, rel)
        return result

    return _guard
