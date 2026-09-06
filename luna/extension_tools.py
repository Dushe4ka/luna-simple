"""Tools the Luna agent can call to add MCP servers or skills (approval-gated)."""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import tool

from luna import mcp, skills
from luna.providers import LunaConfigError
from luna.registry import known_mcp, known_skills, resolve_mcp


@tool
def manage_mcp(
    action: Literal["add", "remove", "list"],
    name: str | None = None,
    command: str | None = None,
    command_args: list[str] | None = None,
) -> str:
    """Add, remove, or list MCP servers for Luna.

    For ``add``: resolve ``name`` from the registry, or pass an explicit
    ``command`` plus ``command_args``. Newly added servers activate after the
    user runs ``/reload``.
    """
    if action == "list":
        configured = ", ".join(mcp.load_mcp_config()) or "(none configured)"
        return f"configured: {configured}\nknown: {', '.join(known_mcp())}"
    if not name:
        return "name is required for add/remove"
    if action == "remove":
        return f"removed {name!r}" if mcp.remove_server(name) else f"{name!r} was not configured"
    try:
        spec = {"command": command, "args": command_args or []} if command else resolve_mcp(name)
    except LunaConfigError as exc:
        return str(exc)
    mcp.add_server(name, spec)
    return f"added MCP server {name!r}. Run /reload to activate."


@tool
def manage_skills(
    action: Literal["add", "remove", "list"],
    name: str | None = None,
    source: str | None = None,
) -> str:
    """Add, remove, or list Luna skills.

    For ``add``: ``source`` is a registry name, ``owner/repo``, or
    ``owner/repo/subdir``. Newly added skills activate after the user runs
    ``/reload``.
    """
    if action == "list":
        installed = ", ".join(f"{s} ({scope})" for scope, s, _ in skills.list_skills())
        return f"installed: {installed or '(none)'}\nknown: {', '.join(known_skills())}"
    if action == "remove":
        return f"removed {name!r}" if name and skills.remove(name) else f"{name!r} is not installed"
    target = source or name
    if not target:
        return "source (or name) is required for add"
    try:
        return skills.install(target, name=name if source else None)
    except LunaConfigError as exc:
        return str(exc)


EXTENSION_TOOLS = [manage_mcp, manage_skills]
EXTENSION_INTERRUPTS = {"manage_mcp": True, "manage_skills": True}
