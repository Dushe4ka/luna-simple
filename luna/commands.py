"""REPL slash-command dispatch table.

``run_repl`` in :mod:`luna.session` delegates every ``/command`` line to
:func:`dispatch`. Each handler takes ``(ctx, arg)`` and either returns a
:class:`DispatchResult` or ``None`` (treated as handled/no-op). Later tasks
register additional handlers by adding an entry to ``_TABLE``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console

from luna.config import LunaConfig
from luna.subagents import subagent_summaries
from luna.ui.theme import PALETTE

HELP: dict[str, str] = {
    "/help": "show this help",
    "/tools": "list the agent's tools",
    "/agents": "list available subagents",
    "/sessions": "list past sessions for this directory",
    "/resume": "resume a past session",
    "/usage": "show token usage this session",
    "/compact": "summarise and compact the conversation",
    "/diff": "show file changes made this session",
    "/undo": "revert the last file change",
    "/add": "pin files into context (/add path ...)",
    "/drop": "unpin files (/drop path ...)",
    "/context": "list pinned files",
    "/verify": "run the project's verify command now",
    "/init": "generate or update AGENTS.md",
    "/model": "show or switch the model (/model <name>)",
    "/provider": "show or switch the provider (/provider <key>)",
    "/reload": "rebuild the agent with the current config",
    "/new": "start a fresh conversation thread",
    "/clear": "clear the screen",
    "/exit": "leave Luna (also /quit, Ctrl-D)",
}


@dataclass
class CommandContext:
    """Everything a slash-command handler might need.

    Fields with defaults are populated by later tasks; adding one here does
    not touch existing call sites.
    """

    console: Console
    config: LunaConfig
    agent: object
    rebuild: Callable[[], object] | None
    thread_id: str
    workdir: str
    index: object | None = None
    session_id: str = ""
    pinned: object | None = None  # context.PinnedFiles, Task 6
    usage: object | None = None  # usage.SessionUsage, Task 5
    permissions: object | None = None  # permissions ruleset, Task 7


@dataclass
class DispatchResult:
    """Outcome of a dispatched line."""

    handled: bool = True
    agent: object | None = None
    thread_id: str | None = None
    exit: bool = False


_TOOL_NAMES = (
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "delete",
    "glob",
    "grep",
    "execute",
    "write_todos",
    "task",
    "manage_mcp",
    "manage_skills",
)


def _print_help(console: Console) -> None:
    for name, help_text in HELP.items():
        console.print(f"  [bold {PALETTE['peri']}]{name}[/]  {help_text}")


def _list_tools(console: Console) -> None:
    console.print("  " + ", ".join(_TOOL_NAMES))
    console.print(f"  [dim {PALETTE['blue']}](+ any MCP tools as mcp__<server>__<tool>)[/]")


def _list_agents(console: Console) -> None:
    for name, desc in subagent_summaries():
        console.print(f"  [bold {PALETTE['accent']}]{name}[/] — {desc}")


def _help(ctx: CommandContext, arg: str) -> None:
    _print_help(ctx.console)


def _tools(ctx: CommandContext, arg: str) -> None:
    _list_tools(ctx.console)


def _agents(ctx: CommandContext, arg: str) -> None:
    _list_agents(ctx.console)


def _clear(ctx: CommandContext, arg: str) -> None:
    ctx.console.clear()


def _reload(ctx: CommandContext, arg: str) -> DispatchResult:
    if ctx.rebuild is None:
        ctx.console.print(f"[{PALETTE['mauve']}]/reload is not available here[/]")
        return DispatchResult()
    new = ctx.rebuild()
    ctx.console.print(f"[{PALETTE['blue']}]reloaded — capabilities refreshed[/]")
    return DispatchResult(agent=new)


def _new(ctx: CommandContext, arg: str) -> DispatchResult:
    tid = uuid.uuid4().hex
    ctx.console.print(f"[{PALETTE['blue']}]started a new thread[/]")
    return DispatchResult(thread_id=tid)


def _startup_only(ctx: CommandContext, name: str) -> None:
    ctx.console.print(
        f"[{PALETTE['blue']}]{name[1:]}: set at startup — restart with --{name[1:]} to change[/]"
    )


def _model(ctx: CommandContext, arg: str) -> None:
    # Task 11 replaces this with real model switching.
    _startup_only(ctx, "/model")


def _provider(ctx: CommandContext, arg: str) -> None:
    # Task 11 replaces this with real provider switching.
    _startup_only(ctx, "/provider")


_TABLE: dict[str, Callable] = {
    "/help": _help,
    "/tools": _tools,
    "/agents": _agents,
    "/clear": _clear,
    "/reload": _reload,
    "/new": _new,
    "/model": _model,
    "/provider": _provider,
    # later tasks register: /sessions /resume /usage /compact /diff /undo
    # /add /drop /context /verify /init
}


def dispatch(line: str, ctx: CommandContext) -> DispatchResult:
    """Route a REPL line. Non-slash lines return ``handled=False``."""
    if not line.startswith("/"):
        return DispatchResult(handled=False)
    name, _, arg = line.partition(" ")
    arg = arg.strip()
    if name in ("/exit", "/quit"):
        return DispatchResult(exit=True)
    handler = _TABLE.get(name)
    if handler is None:
        ctx.console.print(f"[{PALETTE['mauve']}]unknown command {name!r}; try /help[/]")
        return DispatchResult()
    return handler(ctx, arg) or DispatchResult()
