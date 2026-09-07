"""wrap_tool_call middleware: enforce deny rules (Task 8 adds file snapshots here)."""

from __future__ import annotations

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from luna.permissions import RuleSet


def tool_guard(rules: RuleSet, workdir: str):
    """Build a ``wrap_tool_call`` middleware that blocks ``deny``-matched calls."""

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
        return handler(request)

    return _guard
