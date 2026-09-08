"""wrap_tool_call middleware: enforce deny rules and snapshot mutating calls."""

from __future__ import annotations

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from luna.permissions import RuleSet
from luna.undo import snapshot

_MUTATING = {"write_file", "edit_file", "delete"}


def tool_guard(rules: RuleSet, workdir: str, session_id: str = ""):
    """Build a ``wrap_tool_call`` middleware that blocks ``deny``-matched calls.

    Non-denied ``write_file`` / ``edit_file`` / ``delete`` calls are snapshotted
    into the per-session undo journal before the tool runs.
    """

    @wrap_tool_call
    def _guard(request, handler):
        call = request.tool_call
        name = call.get("name", "")
        args = call.get("args", {}) or {}
        if rules.match(name, args) == "deny":
            return ToolMessage(
                content=f"blocked by a Luna permission rule ({name})",
                tool_call_id=call.get("id", "blocked"),
            )
        if session_id and name in _MUTATING:
            rel = (args.get("file_path") or args.get("path") or "").lstrip("/")
            if rel:
                try:
                    snapshot(workdir, session_id, name, rel)
                except (OSError, ValueError):
                    pass
        return handler(request)

    return _guard
