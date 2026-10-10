"""Built-in tools and the tool API."""

from __future__ import annotations

from luna_core.tools.base import Tool, ToolContext, ToolOutput, ToolSpec, args_preview, tool
from luna_core.tools.files import FILE_TOOLS


def builtin_tools() -> list[Tool]:
    """Fresh list of the built-in tools (files now; shell, todos and tasks join later)."""
    return [*FILE_TOOLS]


__all__ = ["Tool", "ToolContext", "ToolOutput", "ToolSpec", "args_preview", "builtin_tools", "tool"]
