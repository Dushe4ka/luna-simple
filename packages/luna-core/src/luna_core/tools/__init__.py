"""Built-in tools and the tool API."""

from __future__ import annotations

from luna_core.tools.base import Tool, ToolContext, ToolOutput, ToolSpec, args_preview, tool
from luna_core.tools.files import FILE_TOOLS


def builtin_tools() -> list[Tool]:
    """Fresh list of the built-in tools (``task`` tools are added by the agent)."""
    from luna_core.tools.shell import execute
    from luna_core.tools.todos import write_todos

    return [*FILE_TOOLS, execute, write_todos]


__all__ = ["Tool", "ToolContext", "ToolOutput", "ToolSpec", "args_preview", "builtin_tools", "tool"]
