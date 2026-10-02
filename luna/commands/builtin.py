"""Built-in slash commands. Strings match the REPL's historical output."""

from __future__ import annotations

import os

from luna.commands.base import Choice, CommandResult, Confirm
from luna.commands.registry import register, register_ui
from luna.config.credentials import get_api_key
from luna.config.providers import LunaConfigError, merge_providers
from luna.turn.engine import Notice
from luna.turn.verify import run_verify

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


def _api_key(provider: str, spec) -> str | None:
    key = os.environ.get(spec.env_var) if spec.env_var else None
    return key or (get_api_key(provider) if spec.env_var else None)


def _models_for(cfg) -> list[str]:
    """Live model ids for the session's provider, else the bundled list."""
    from luna.config import model_discovery

    spec = merge_providers(cfg.custom_providers)[cfg.provider]
    models = model_discovery.list_models(cfg.provider, spec, api_key=_api_key(cfg.provider, spec))
    return models if models is not None else model_discovery.known_models(cfg.provider)


def _is_git(workdir: str) -> bool:
    from luna.turn import gitinfo

    return gitinfo.is_git_repo(workdir)


def peek_last(workdir, session_id):
    """Lazy seam over :func:`luna.turn.undo.peek_last` (patched in tests)."""
    from luna.turn.undo import peek_last as _peek

    return _peek(workdir, session_id)


def undo_last(workdir, session_id):
    """Lazy seam over :func:`luna.turn.undo.undo_last`."""
    from luna.turn.undo import undo_last as _undo

    return _undo(workdir, session_id)


def forget_messages(workdir, session_id, count):
    """Lazy seam over :func:`luna.turn.undo.forget_messages`."""
    from luna.turn.undo import forget_messages as _forget

    return _forget(workdir, session_id, count)


def compact_history(agent, thread_id):
    """Lazy seam over :func:`luna.core.session.compact_history` (avoids an import cycle)."""
    from luna.core.session import compact_history as _compact

    return _compact(agent, thread_id)


def init_prompt(workdir):
    """Lazy seam over :func:`luna.extensions.initgen.init_prompt`."""
    from luna.extensions.initgen import init_prompt as _init

    return _init(workdir)


def _message_count(env) -> int:
    try:
        config = {"configurable": {"thread_id": env.thread_id}}
        return len(env.agent.get_state(config).values.get("messages", []))
    except Exception:  # noqa: BLE001 - best-effort ledger resync
        return 0


register_ui("/help", "show this help")


@register("/tools", "list the agent's tools", "read")
def _tools(env, arg):
    return CommandResult(
        text=", ".join(TOOL_NAMES) + "\n\n(+ any MCP tools as `mcp__<server>__<tool>`)"
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
def _compact(env, arg):
    try:
        ok, message = compact_history(env.agent, env.thread_id)
    except Exception as exc:  # noqa: BLE001 - a failed compact must not kill the session
        return CommandResult(notices=[Notice("error", f"/compact failed: {exc}")])
    if ok:
        forget_messages(env.workdir, env.session_id, _message_count(env))
    if env.index is not None:
        env.index.touch(env.thread_id)
    return CommandResult(notices=[Notice("info" if ok else "error", message)])


@register("/diff", "show file changes made this session", "read")
def _diff(env, arg):
    from luna.turn.undo import session_diff

    text = session_diff(env.workdir, env.session_id)
    if not text:
        return CommandResult(notices=[Notice("dim", "no changes this session")])
    return CommandResult(text=f"```diff\n{text}\n```")


@register("/undo", "revert the last file change", "mutate")
def _undo(env, arg):
    yes = arg.strip() == "--yes"
    try:
        if _is_git(env.workdir):
            if not yes:
                return CommandResult(
                    confirm=Confirm(
                        "undo the last turn (files + conversation)?",
                        "/undo --yes",
                        "undo cancelled",
                    )
                )
            from luna.turn.undo import undo as git_undo

            note = git_undo(env.workdir, env.session_id, env.agent, env.thread_id)
        else:
            desc = peek_last(env.workdir, env.session_id)
            if desc is None:
                return CommandResult(notices=[Notice("dim", "nothing to undo")])
            if not yes:
                return CommandResult(confirm=Confirm(f"{desc}?", "/undo --yes", "undo cancelled"))
            note = undo_last(env.workdir, env.session_id)
    except Exception as exc:  # noqa: BLE001 - a failed undo must not kill the session
        return CommandResult(notices=[Notice("error", f"/undo failed: {exc}")])
    return CommandResult(
        notices=[Notice("info", note) if note else Notice("dim", "nothing to undo")]
    )


@register("/redo", "re-apply the last undone turn (git only)", "mutate")
def _redo(env, arg):
    try:
        if not _is_git(env.workdir):
            return CommandResult(notices=[Notice("dim", "redo needs a git repository")])
        from luna.turn.undo import redo as git_redo

        note = git_redo(env.workdir, env.session_id, env.agent, env.thread_id)
    except Exception as exc:  # noqa: BLE001 - a failed redo must not kill the session
        return CommandResult(notices=[Notice("error", f"/redo failed: {exc}")])
    return CommandResult(
        notices=[Notice("info", note) if note else Notice("dim", "nothing to redo")]
    )


@register("/add", "pin files into context (/add path ...)", "mutate")
def _add(env, arg):
    if not arg:
        return CommandResult(notices=[Notice("dim", "usage: /add path ...")])
    env.pinned.add(*arg.split())
    return CommandResult(
        notices=[Notice("dim", f"pinned: {', '.join(env.pinned.paths)}")],
        effects={"pinned": len(env.pinned.paths)},
    )


@register("/drop", "unpin files (/drop path ...)", "mutate")
def _drop(env, arg):
    env.pinned.drop(*arg.split())
    return CommandResult(
        notices=[Notice("dim", f"pinned: {', '.join(env.pinned.paths) or '(none)'}")],
        effects={"pinned": len(env.pinned.paths)},
    )


@register("/context", "list pinned files", "read")
def _context(env, arg):
    if not env.pinned.paths:
        return CommandResult(notices=[Notice("dim", "(no pinned files)")])
    return CommandResult(text="\n".join(env.pinned.paths))


@register("/verify", "run the project's verify command now", "mutate")
def _verify(env, arg):
    if not env.config.verify_command:
        return CommandResult(notices=[Notice("dim", "set agent.verify_command in config first")])
    ok, tail = run_verify(env.config.verify_command, env.workdir)
    return CommandResult(
        notices=[Notice("ok", "✓ verify ok") if ok else Notice("warn", f"verify failed\n{tail}")]
    )


@register("/diagnose", "run the project's diagnostics command now", "mutate")
def _diagnose(env, arg):
    from luna.turn import diagnose

    cmd = env.config.diagnose_command
    if cmd == "auto":
        cmd = diagnose.detect(env.workdir)
    if not cmd:
        return CommandResult(notices=[Notice("dim", "no diagnose command configured or detected")])
    text = diagnose.run(cmd, env.workdir, [])
    return (
        CommandResult(text=text) if text else CommandResult(notices=[Notice("dim", "no findings")])
    )


@register("/init", "generate or update AGENTS.md", "prompt")
def _init(env, arg):
    env.after_turn_reload()
    return CommandResult(prompt=init_prompt(env.workdir))


@register("/model", "pick a model interactively, or /model <name> to switch directly", "mutate")
def _model(env, arg):
    if not arg:
        current = Notice("dim", f"model: {env.config.model or '(provider default)'}")
        models = _models_for(env.config)
        if not models:
            return CommandResult(notices=[current])
        return CommandResult(
            notices=[current],
            choice=Choice(
                f"Модель ({env.config.provider})",
                [(m, f"{m}  ✓ текущая" if m == env.config.model else m) for m in models],
                "/model {value}",
            ),
        )
    try:
        env.switch_model(arg)
    except Exception as exc:  # noqa: BLE001 - a bad model must not kill the session
        return CommandResult(notices=[Notice("error", f"could not switch: {exc}")])
    return CommandResult(notices=[Notice("info", f"model → {arg}")], effects={"model": arg})


@register("/provider", "show or switch the provider (/provider <key>)", "mutate")
def _provider(env, arg):
    registry = merge_providers(env.config.custom_providers)
    if not arg:
        current = Notice("dim", f"provider: {env.config.provider}")
        usable = [k for k, spec in registry.items() if not spec.env_var or _api_key(k, spec)]
        if not usable:
            hint = Notice("dim", "нет провайдеров с ключом — luna config set-key <провайдер>")
            return CommandResult(notices=[current, hint])
        return CommandResult(
            notices=[current],
            choice=Choice("Провайдер", [(k, k) for k in usable], "/provider {value}"),
        )
    if arg not in registry:
        return CommandResult(notices=[Notice("error", f"unknown provider {arg!r}")])
    spec = registry[arg]
    if spec.env_var and not _api_key(arg, spec):
        return CommandResult(
            notices=[Notice("error", f"no key for {arg}; run: luna config set-key {arg}")]
        )
    try:
        env.switch_provider(arg)
    except Exception as exc:  # noqa: BLE001 - a bad provider must not kill the session
        return CommandResult(notices=[Notice("error", f"could not switch: {exc}")])
    return CommandResult(notices=[Notice("info", f"provider → {arg}")], effects={"provider": arg})


@register("/reload", "rebuild the agent with the current config", "mutate")
def _reload(env, arg):
    if not env.can_rebuild:
        return CommandResult(notices=[Notice("error", "/reload is not available here")])
    try:
        env.rebuild()
    except Exception as exc:  # noqa: BLE001 - a bad config must not kill the session
        return CommandResult(notices=[Notice("error", f"/reload failed: {exc}")])
    return CommandResult(
        notices=[Notice("info", "reloaded — capabilities refreshed")],
        effects={"agent_rebuilt": True},
    )


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
def _plan(env, arg):
    current = env.get_plan()
    if current is None:
        return CommandResult(notices=[Notice("dim", "plan mode is not available here")])
    on = arg == "on" if arg in ("on", "off") else not current
    env.set_plan(on)
    return CommandResult(
        notices=[Notice("info", f"plan mode: {'on' if on else 'off'}")], effects={"plan": on}
    )


register_ui("/clear", "clear the screen")
