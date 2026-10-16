"""App tools the Luna agent uses to extend itself (approval-gated)."""

from __future__ import annotations

import asyncio
from typing import Literal

from luna_core.events import ReloadRequested
from luna_core.tools.base import ToolContext, ToolOutput, tool

from luna.config.providers import LunaConfigError
from luna.extensions import mcp, skills
from luna.extensions.registry import known_mcp, known_skills, resolve_mcp
from luna.extensions.websearch import web_search


@tool(mutating=True)
async def manage_mcp(
    ctx: ToolContext,
    action: Literal["add", "remove", "list"],
    name: str | None = None,
    command: str | None = None,
    command_args: list[str] | None = None,
) -> ToolOutput:
    """Add, remove or list MCP servers (add: a registry name, or command + command_args).

    Args:
        ctx: Tool context (workspace root, event sink).
        action: add, remove or list.
        name: Server name.
        command: Executable for a custom stdio server.
        command_args: Its arguments.

    """
    root = str(ctx.workspace.root)
    if action == "list":
        configured = ", ".join(mcp.load_mcp_config(root)) or "(none configured)"
        return ToolOutput.text(f"configured: {configured}\nknown: {', '.join(known_mcp())}")
    if not name:
        return ToolOutput.error("name is required for add/remove")
    if action == "remove":
        removed = await asyncio.to_thread(mcp.remove_server, name, workdir=root)
        if removed:
            ctx.emit(ReloadRequested())
        return ToolOutput.text(f"removed {name!r}" if removed else f"{name!r} was not configured")
    try:
        spec = {"command": command, "args": command_args or []} if command else resolve_mcp(name)
    except LunaConfigError as exc:
        return ToolOutput.error(str(exc))
    await asyncio.to_thread(mcp.add_server, name, spec, workdir=root)
    ctx.emit(ReloadRequested())
    return ToolOutput.text(f"added MCP server {name!r}; it is loaded automatically after this turn")


@tool(mutating=True)
async def manage_skills(
    ctx: ToolContext,
    action: Literal["add", "remove", "list"],
    name: str | None = None,
    source: str | None = None,
) -> ToolOutput:
    """Add, remove or list skills (add: a registry name, owner/repo or owner/repo/subdir).

    Args:
        ctx: Tool context (workspace root, event sink).
        action: add, remove or list.
        name: Skill name.
        source: Where to install it from.

    """
    root = str(ctx.workspace.root)
    if action == "list":
        installed = ", ".join(f"{s} ({scope})" for scope, s, _ in skills.list_skills(root))
        return ToolOutput.text(
            f"installed: {installed or '(none)'}\nknown: {', '.join(known_skills())}"
        )
    if action == "remove":
        if name and await asyncio.to_thread(skills.remove, name, workdir=root):
            ctx.emit(ReloadRequested())
            return ToolOutput.text(f"removed {name!r}")
        return ToolOutput.error(f"{name!r} is not installed")
    target = source or name
    if not target:
        return ToolOutput.error("source (or name) is required for add")
    try:
        message = await asyncio.to_thread(
            skills.install, target, name=name if source else None, workdir=root
        )
    except LunaConfigError as exc:
        return ToolOutput.error(str(exc))
    ctx.emit(ReloadRequested())
    return ToolOutput.text(message)


EXTENSION_TOOLS = [manage_mcp, manage_skills, web_search]
EXTENSION_APPROVALS = frozenset({"manage_mcp", "manage_skills", "remember", "save_skill"})
