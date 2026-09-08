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
from luna.undo import session_diff, undo_last
from luna.verify import run_verify

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


def _usage(ctx: CommandContext, arg: str) -> None:
    session = ctx.usage
    if session is None or not getattr(session, "turns", None):
        ctx.console.print(f"[{PALETTE['blue']}]no usage recorded yet[/]")
        return
    in_tok, out_tok, total = session.totals
    ctx.console.print(
        f"  turns: {len(session.turns)}  in: {in_tok}  out: {out_tok}  total: {total}"
    )


def _model(ctx: CommandContext, arg: str) -> None:
    # Task 11 replaces this with real model switching.
    _startup_only(ctx, "/model")


def _provider(ctx: CommandContext, arg: str) -> None:
    # Task 11 replaces this with real provider switching.
    _startup_only(ctx, "/provider")


_COMPACT_ASK = (
    "Summarise this whole session as a dense handoff note: the goal, decisions "
    "made, files touched, current state, and open questions. No preamble."
)


def _compact(ctx: CommandContext, arg: str) -> DispatchResult:
    old_config = {"configurable": {"thread_id": ctx.thread_id}}
    result = ctx.agent.invoke(
        {"messages": [{"role": "user", "content": _COMPACT_ASK}]}, config=old_config
    )
    summary = ""
    for msg in reversed(result.get("messages", [])):
        text = getattr(msg, "content", "")
        if getattr(msg, "type", "") == "ai" and isinstance(text, str) and text.strip():
            summary = text.strip()
            break
    new_id = uuid.uuid4().hex
    ctx.agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Continuing a compacted session. Handoff note:\n" + summary,
                }
            ]
        },
        config={"configurable": {"thread_id": new_id}},
    )
    if ctx.index is not None:
        title = "compacted"
        row = None
        try:
            row = next(
                (r for r in ctx.index.list(ctx.workdir) if r.thread_id == ctx.thread_id),
                None,
            )
        except Exception:  # noqa: BLE001 - index is best-effort here
            row = None
        if row is not None:
            title = "compacted: " + row.title
        ctx.index.record(new_id, ctx.workdir, title)
        ctx.index.touch(new_id)
    ctx.console.print(f"[{PALETTE['blue']}]compacted — new thread seeded from the summary[/]")
    return DispatchResult(thread_id=new_id)


def _add(ctx: CommandContext, arg: str) -> None:
    if not arg:
        ctx.console.print("[dim]usage: /add path ...[/]")
        return
    ctx.pinned.add(*arg.split())
    ctx.console.print(f"[dim]pinned: {', '.join(ctx.pinned.paths)}[/]")


def _drop(ctx: CommandContext, arg: str) -> None:
    ctx.pinned.drop(*arg.split())
    ctx.console.print(f"[dim]pinned: {', '.join(ctx.pinned.paths) or '(none)'}[/]")


def _context(ctx: CommandContext, arg: str) -> None:
    ctx.console.print("\n".join(f"  {p}" for p in ctx.pinned.paths) or "[dim](no pinned files)[/]")


def _verify(ctx: CommandContext, arg: str) -> None:
    """Run the project's verify command now."""
    if not ctx.config.verify_command:
        ctx.console.print("[dim]set agent.verify_command in config first[/]")
        return
    ok, tail = run_verify(ctx.config.verify_command, ctx.config.workdir)
    ctx.console.print("[dim]✓ verify ok[/]" if ok else f"[yellow]verify failed[/]\n{tail}")


def _diff(ctx: CommandContext, arg: str) -> None:
    """Show the diff of files changed this session."""
    text = session_diff(ctx.workdir, ctx.session_id)
    ctx.console.print(text or "[dim]no changes this session[/]")


def _undo(ctx: CommandContext, arg: str) -> None:
    """Revert the last file change made this session."""
    note = undo_last(ctx.workdir, ctx.session_id)
    ctx.console.print(f"[{PALETTE['blue']}]{note}[/]" if note else "[dim]nothing to undo[/]")


_TABLE: dict[str, Callable] = {
    "/help": _help,
    "/tools": _tools,
    "/agents": _agents,
    "/clear": _clear,
    "/reload": _reload,
    "/new": _new,
    "/compact": _compact,
    "/usage": _usage,
    "/model": _model,
    "/provider": _provider,
    "/add": _add,
    "/drop": _drop,
    "/context": _context,
    "/diff": _diff,
    "/undo": _undo,
    "/verify": _verify,
    # later tasks register: /sessions /resume /init
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
