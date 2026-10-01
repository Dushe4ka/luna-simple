"""Built-in slash commands. Strings match the REPL's historical output."""

from __future__ import annotations

from luna.commands.base import CommandResult
from luna.commands.registry import register, register_ui
from luna.config.providers import LunaConfigError
from luna.turn.engine import Notice

TOOL_NAMES = (
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
    "save_skill",
    "web_search",
)

register_ui("/help", "show this help")


@register("/tools", "list the agent's tools", "read")
def _tools(env, arg):
    return CommandResult(
        text=", ".join(TOOL_NAMES) + "\n\n*(+ any MCP tools as mcp__<server>__<tool>)*"
    )


@register("/agents", "list available subagents", "read")
def _agents(env, arg):
    from luna.extensions.subagents import subagent_summaries

    try:
        summaries = subagent_summaries(env.workdir)
    except LunaConfigError as exc:
        return CommandResult(notices=[Notice("error", str(exc))])
    if not summaries:
        return CommandResult(notices=[Notice("dim", "(no subagents)")])
    return CommandResult(text="\n".join(f"- **{n}** — {d}" for n, d in summaries))


register_ui("/sessions", "list past sessions for this directory")
register_ui("/resume", "resume a past session")


@register("/usage", "show token usage this session", "read")
def _usage(env, arg):
    if not env.usage.turns:
        return CommandResult(notices=[Notice("info", "no usage recorded yet")])
    in_tok, out_tok, total = env.usage.totals
    lines = [f"turns: {len(env.usage.turns)}  in: {in_tok}  out: {out_tok}  total: {total}"]
    cost = env.usage.cost(env.config.provider, env.config.model, env.config.pricing)
    if cost is not None:
        lines.append(f"cost: ${cost:.4f}")
    return CommandResult(text="\n".join(lines))


@register("/compact", "summarise and compact the conversation", "mutate")
def _stub_compact(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


@register("/diff", "show file changes made this session", "read")
def _diff(env, arg):
    from luna.turn.undo import session_diff

    text = session_diff(env.workdir, env.session_id)
    if not text:
        return CommandResult(notices=[Notice("dim", "no changes this session")])
    return CommandResult(text=f"```diff\n{text}\n```")


@register("/undo", "revert the last file change", "mutate")
def _stub_undo(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


@register("/redo", "re-apply the last undone turn (git only)", "mutate")
def _stub_redo(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


@register("/add", "pin files into context (/add path ...)", "mutate")
def _stub_add(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


@register("/drop", "unpin files (/drop path ...)", "mutate")
def _stub_drop(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


@register("/context", "list pinned files", "read")
def _context(env, arg):
    if not env.pinned.paths:
        return CommandResult(notices=[Notice("dim", "(no pinned files)")])
    return CommandResult(text="\n".join(env.pinned.paths))


@register("/verify", "run the project's verify command now", "mutate")
def _stub_verify(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


@register("/diagnose", "run the project's diagnostics command now", "mutate")
def _stub_diagnose(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


@register("/init", "generate or update AGENTS.md", "prompt")
def _stub_init(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


@register("/model", "pick a model interactively, or /model <name> to switch directly", "mutate")
def _stub_model(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


@register("/provider", "show or switch the provider (/provider <key>)", "mutate")
def _stub_provider(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


@register("/reload", "rebuild the agent with the current config", "mutate")
def _stub_reload(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


register_ui("/new", "start a fresh conversation thread")


@register("/commands", "list custom slash commands", "read")
def _commands(env, arg):
    cmds = env.user_commands or {}
    if not cmds:
        return CommandResult(notices=[Notice("dim", "(no custom commands)")])
    return CommandResult(
        text="\n".join(f"- `/{n}` — {c.description}" for n, c in sorted(cmds.items()))
    )


@register("/plan", "toggle plan mode (blocks writes/execute)", "mutate")
def _stub_plan(env, arg):
    return CommandResult(notices=[Notice("error", "not implemented")])


register_ui("/clear", "clear the screen")
