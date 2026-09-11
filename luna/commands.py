"""REPL slash-command dispatch table.

``run_repl`` in :mod:`luna.session` delegates every ``/command`` line to
:func:`dispatch`. Each handler takes ``(ctx, arg)`` and either returns a
:class:`DispatchResult` or ``None`` (treated as handled/no-op). Later tasks
register additional handlers by adding an entry to ``_TABLE``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console

from luna.config import LunaConfig
from luna.credentials import get_api_key
from luna.providers import PROVIDERS, LunaConfigError
from luna.subagents import subagent_summaries
from luna.ui.theme import PALETTE
from luna.undo import peek_last, session_diff, undo_last
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
    "/redo": "re-apply the last undone turn (git only)",
    "/add": "pin files into context (/add path ...)",
    "/drop": "unpin files (/drop path ...)",
    "/context": "list pinned files",
    "/verify": "run the project's verify command now",
    "/diagnose": "run the project's diagnostics command now",
    "/init": "generate or update AGENTS.md",
    "/model": "show or switch the model (/model <name>)",
    "/provider": "show or switch the provider (/provider <key>)",
    "/reload": "rebuild the agent with the current config",
    "/new": "start a fresh conversation thread",
    "/commands": "list custom slash commands",
    "/plan": "toggle plan mode (blocks writes/execute)",
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
    input_fn: Callable[[str], str] | None = None
    user_commands: dict | None = None  # usercmd.load(workdir), Task 5
    plan_state: list | None = None


@dataclass
class DispatchResult:
    """Outcome of a dispatched line."""

    handled: bool = True
    agent: object | None = None
    thread_id: str | None = None
    exit: bool = False
    prompt: str | None = None


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


def _list_agents(console: Console, workdir: str = ".") -> None:
    try:
        summaries = subagent_summaries(workdir)
    except LunaConfigError as exc:
        # A bad subagents.toml (e.g. a mutating subagent without unsafe = true)
        # must not unwind past dispatch() and kill the REPL. /reload reports the
        # same error the same way.
        console.print(f"[{PALETTE['mauve']}]{exc}[/]")
        return
    for name, desc in summaries:
        console.print(f"  [bold {PALETTE['accent']}]{name}[/] — {desc}")


def _help(ctx: CommandContext, arg: str) -> None:
    _print_help(ctx.console)


def _tools(ctx: CommandContext, arg: str) -> None:
    _list_tools(ctx.console)


def _agents(ctx: CommandContext, arg: str) -> None:
    _list_agents(ctx.console, ctx.workdir)


def _clear(ctx: CommandContext, arg: str) -> None:
    ctx.console.clear()


def _reload(ctx: CommandContext, arg: str) -> DispatchResult | None:
    if ctx.rebuild is None:
        ctx.console.print(f"[{PALETTE['mauve']}]/reload is not available here[/]")
        return DispatchResult()
    try:
        new = ctx.rebuild()
    except Exception as exc:  # noqa: BLE001 - a bad config must not kill the REPL
        ctx.console.print(f"[{PALETTE['mauve']}]/reload failed: {exc}[/]")
        return None
    ctx.console.print(f"[{PALETTE['blue']}]reloaded — capabilities refreshed[/]")
    return DispatchResult(agent=new)


def _new(ctx: CommandContext, arg: str) -> DispatchResult:
    tid = uuid.uuid4().hex
    ctx.console.print(f"[{PALETTE['blue']}]started a new thread[/]")
    return DispatchResult(thread_id=tid)


def _sessions(ctx: CommandContext, arg: str) -> None:
    """List past sessions for this directory."""
    if ctx.index is None:
        ctx.console.print("[dim]session history is not available here[/]")
        return
    rows = ctx.index.list(ctx.workdir)
    if not rows:
        ctx.console.print("[dim]no sessions recorded for this directory[/]")
        return
    for n, r in enumerate(rows, 1):
        ctx.console.print(f"  [{n}] {r.title}")


def _resume(ctx: CommandContext, arg: str) -> DispatchResult | None:
    """Resume a past session: /resume <number> (no arg lists them)."""
    if ctx.index is None:
        ctx.console.print("[dim]session history is not available here[/]")
        return None
    rows = ctx.index.list(ctx.workdir)
    if not arg:
        if not rows:
            ctx.console.print("[dim]no sessions recorded for this directory[/]")
            return None
        for n, r in enumerate(rows, 1):
            ctx.console.print(f"  [{n}] {r.title}")
        ctx.console.print("[dim]usage: /resume <number>[/]")
        return None
    target = None
    if arg.isdigit() and 1 <= int(arg) <= len(rows):
        target = rows[int(arg) - 1].thread_id
    else:
        target = arg  # treat as a thread id
    ctx.console.print(f"[{PALETTE['blue']}]resumed session {target[:8]}[/]")
    return DispatchResult(thread_id=target)


def _usage(ctx: CommandContext, arg: str) -> None:
    session = ctx.usage
    if session is None or not getattr(session, "turns", None):
        ctx.console.print(f"[{PALETTE['blue']}]no usage recorded yet[/]")
        return
    in_tok, out_tok, total = session.totals
    ctx.console.print(
        f"  turns: {len(session.turns)}  in: {in_tok}  out: {out_tok}  total: {total}"
    )
    cost = session.cost(ctx.config.provider, ctx.config.model, ctx.config.pricing)
    if cost is not None:
        ctx.console.print(f"  cost: ${cost:.4f}")


def _model(ctx: CommandContext, arg: str) -> DispatchResult | None:
    if not arg:
        ctx.console.print(f"model: {ctx.config.model or '(provider default)'}")
        return None
    previous_model = ctx.config.model
    ctx.config.model = arg
    try:
        new_agent = ctx.rebuild()
    except Exception as exc:  # noqa: BLE001 - a bad model must not kill the REPL
        ctx.config.model = previous_model
        ctx.console.print(f"[{PALETTE['mauve']}]could not switch: {exc}[/]")
        return None
    ctx.console.print(f"[{PALETTE['blue']}]model → {arg}[/]")
    return DispatchResult(agent=new_agent)


def _provider(ctx: CommandContext, arg: str) -> DispatchResult | None:
    if not arg:
        ctx.console.print(f"provider: {ctx.config.provider}")
        return None
    if arg not in PROVIDERS:
        ctx.console.print(f"[{PALETTE['mauve']}]unknown provider {arg!r}[/]")
        return None
    spec = PROVIDERS[arg]
    if spec.env_var and not (os.environ.get(spec.env_var) or get_api_key(arg)):
        ctx.console.print(
            f"[{PALETTE['mauve']}]no key for {arg}; run: luna config set-key {arg}[/]"
        )
        return None
    previous_provider = ctx.config.provider
    previous_model = ctx.config.model
    ctx.config.provider = arg
    ctx.config.model = None
    try:
        new_agent = ctx.rebuild()
    except Exception as exc:  # noqa: BLE001 - a bad provider must not kill the REPL
        ctx.config.provider = previous_provider
        ctx.config.model = previous_model
        ctx.console.print(f"[{PALETTE['mauve']}]could not switch: {exc}[/]")
        return None
    ctx.console.print(f"[{PALETTE['blue']}]provider → {arg}[/]")
    return DispatchResult(agent=new_agent)


def _compact(ctx: CommandContext, arg: str) -> DispatchResult | None:
    """Summarise the conversation and replace its history in place."""
    from luna import undo
    from luna.session import compact_thread  # lazy: session imports commands

    try:
        compact_thread(ctx.agent, ctx.thread_id, ctx.console)
    except Exception as exc:  # noqa: BLE001 - a failed compact must not kill the REPL
        ctx.console.print(f"[{PALETTE['mauve']}]/compact failed: {exc}[/]")
    else:
        try:
            config = {"configurable": {"thread_id": ctx.thread_id}}
            count = len(ctx.agent.get_state(config).values.get("messages", []))
        except Exception:  # noqa: BLE001 - best-effort ledger resync
            count = 0
        undo.forget_messages(ctx.workdir, ctx.session_id, count)
    if ctx.index is not None:
        ctx.index.touch(ctx.thread_id)
    return None


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


def _diagnose(ctx: CommandContext, arg: str) -> None:
    """Run the project's diagnostics command now."""
    from luna import diagnose

    cmd = ctx.config.diagnose_command
    if cmd == "auto":
        cmd = diagnose.detect(ctx.config.workdir)
    if not cmd:
        ctx.console.print("[dim]no diagnose command configured or detected[/]")
        return
    text = diagnose.run(cmd, ctx.config.workdir, [])
    ctx.console.print(text or "[dim]no findings[/]")


def _diff(ctx: CommandContext, arg: str) -> None:
    """Show the diff of files changed this session."""
    text = session_diff(ctx.workdir, ctx.session_id)
    ctx.console.print(text or "[dim]no changes this session[/]")


def _undo(ctx: CommandContext, arg: str) -> None:
    """Revert the last file change made this session (confirms first)."""
    from luna import gitinfo

    try:
        if gitinfo.is_git_repo(ctx.workdir):
            from luna.undo import undo as git_undo

            if ctx.input_fn is not None:
                answer = (
                    ctx.input_fn("undo the last turn (files + conversation)? [y/N] ")
                    .strip()
                    .lower()
                )
                if answer not in ("y", "yes"):
                    ctx.console.print("[dim]undo cancelled[/]")
                    return
            note = git_undo(ctx.workdir, ctx.session_id, ctx.agent, ctx.thread_id)
            ctx.console.print(
                f"[{PALETTE['blue']}]{note}[/]" if note else "[dim]nothing to undo[/]"
            )
            return
        desc = peek_last(ctx.workdir, ctx.session_id)
        if desc is None:
            ctx.console.print("[dim]nothing to undo[/]")
            return
        if ctx.input_fn is not None:
            answer = ctx.input_fn(f"{desc}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                ctx.console.print("[dim]undo cancelled[/]")
                return
        note = undo_last(ctx.workdir, ctx.session_id)
        ctx.console.print(f"[{PALETTE['blue']}]{note}[/]" if note else "[dim]nothing to undo[/]")
    except Exception as exc:  # noqa: BLE001 - a failed undo must not kill the REPL
        ctx.console.print(f"[{PALETTE['mauve']}]/undo failed: {exc}[/]")


def _redo(ctx: CommandContext, arg: str) -> None:
    """Re-apply the last undone turn (git repositories only)."""
    from luna import gitinfo

    try:
        if not gitinfo.is_git_repo(ctx.workdir):
            ctx.console.print("[dim]redo needs a git repository[/]")
            return
        from luna.undo import redo as git_redo

        note = git_redo(ctx.workdir, ctx.session_id, ctx.agent, ctx.thread_id)
        ctx.console.print(f"[{PALETTE['blue']}]{note}[/]" if note else "[dim]nothing to redo[/]")
    except Exception as exc:  # noqa: BLE001 - a failed redo must not kill the REPL
        ctx.console.print(f"[{PALETTE['mauve']}]/redo failed: {exc}[/]")


def _init(ctx: CommandContext, arg: str) -> DispatchResult | None:
    """Generate or update AGENTS.md for this repo."""
    from luna.initgen import init_prompt
    from luna.session import run_once  # lazy: session imports commands

    run_once(
        ctx.agent,
        init_prompt(ctx.workdir),
        thread_id=ctx.thread_id,
        console=ctx.console,
        workdir=ctx.workdir,
    )
    if ctx.rebuild is not None:
        return DispatchResult(agent=ctx.rebuild())
    return None


def _commands(ctx: CommandContext, arg: str) -> None:
    """List custom slash commands loaded from .luna/commands/."""
    cmds = ctx.user_commands or {}
    if not cmds:
        ctx.console.print("[dim](no custom commands)[/]")
        return
    for name, cmd in sorted(cmds.items()):
        ctx.console.print(f"  [bold {PALETTE['peri']}]/{name}[/]  {cmd.description}")


def _plan(ctx: CommandContext, arg: str) -> None:
    """Toggle plan mode: /plan, /plan on, /plan off."""
    if ctx.plan_state is None:
        ctx.console.print("[dim]plan mode is not available here[/]")
        return
    if arg in ("on", "off"):
        ctx.plan_state[0] = arg == "on"
    else:
        ctx.plan_state[0] = not ctx.plan_state[0]
    ctx.console.print(f"[{PALETTE['blue']}]plan mode: {'on' if ctx.plan_state[0] else 'off'}[/]")


_TABLE: dict[str, Callable] = {
    "/help": _help,
    "/tools": _tools,
    "/agents": _agents,
    "/clear": _clear,
    "/reload": _reload,
    "/new": _new,
    "/sessions": _sessions,
    "/resume": _resume,
    "/compact": _compact,
    "/usage": _usage,
    "/model": _model,
    "/provider": _provider,
    "/add": _add,
    "/drop": _drop,
    "/context": _context,
    "/diff": _diff,
    "/undo": _undo,
    "/redo": _redo,
    "/verify": _verify,
    "/diagnose": _diagnose,
    "/init": _init,
    "/commands": _commands,
    "/plan": _plan,
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
        bare = name[1:]
        if ctx.user_commands and bare in ctx.user_commands:
            from luna.usercmd import expand

            prompt = expand(ctx.user_commands[bare], arg, ctx.workdir)
            return DispatchResult(prompt=prompt)
        ctx.console.print(f"[{PALETTE['mauve']}]unknown command {name!r}; try /help[/]")
        return DispatchResult()
    return handler(ctx, arg) or DispatchResult()
