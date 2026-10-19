"""Built-in slash commands. Strings match the REPL's historical output."""

from __future__ import annotations

import asyncio
import os

from luna_core.errors import LunaCoreError, ProviderError
from luna_core.events import Compacted
from luna_core.loop import readable_error

from luna.commands.base import Choice, CommandResult, Confirm
from luna.commands.registry import register, register_ui
from luna.config.credentials import get_api_key
from luna.config.providers import LunaConfigError, merge_providers
from luna.turn.engine import Notice
from luna.turn.verify import run_verify


def _api_key(provider: str, spec) -> str | None:
    key = os.environ.get(spec.env_var) if spec.env_var else None
    return key or (get_api_key(provider) if spec.env_var else None)


def _models_for(cfg) -> list[str]:
    """Live model ids for the session's provider, else the bundled list."""
    from luna.config import model_discovery

    spec = merge_providers(cfg.custom_providers)[cfg.provider]
    models = model_discovery.list_models(cfg.provider, spec, api_key=_api_key(cfg.provider, spec))
    return models if models is not None else model_discovery.known_models(cfg.provider)


def init_prompt(workdir):
    """Lazy seam over :func:`luna.extensions.initgen.init_prompt`."""
    from luna.extensions.initgen import init_prompt as _init

    return _init(workdir)


register_ui("/help", "show this help")


@register("/tools", "list the agent's tools", "read", agent=True)
def _tools(env, arg):
    return CommandResult(text=", ".join(sorted(env.agent.tools)))


@register("/agents", "subagents and background work", "read", agent=True)
def _agents(env, arg):
    from luna.extensions.subagents import subagent_summaries

    try:
        summaries = subagent_summaries(env.workdir)
    except LunaConfigError as exc:
        return CommandResult(notices=[Notice("error", str(exc))])
    lines = [f"- **{n}** — {d}" for n, d in summaries] or ["(no subagents)"]
    runs = env.manager.list() if env.manager is not None else []
    background = env.agent.services.get("background")
    tasks = background.list(env.session.id) if background is not None else []
    work = [f"- {r.session_id[:8]} · {r.label} · {r.status} · ${r.cost:.3f}" for r in runs]
    work += [f"- {t.handle} · {t.label} · {t.status}" for t in tasks]
    text = "**Subagents**\n" + "\n".join(lines)
    return CommandResult(
        text=text + "\n\n**Background**\n" + ("\n".join(work) or "(no background work)")
    )


register_ui("/sessions", "list past sessions for this directory")
register_ui("/resume", "resume a past session")


@register("/usage", "show token usage and cost this session", "read")
def _usage(env, arg):
    usage = env.session.usage()
    if not usage.total:
        return CommandResult(notices=[Notice("info", "no usage recorded yet")])
    turns = len(env.session.turn_ids())
    lines = [
        f"turns: {turns}  in: {usage.input}  out: {usage.output}  "
        f"cached: {usage.cache_read}  total: {usage.total}"
    ]
    cost = env.session.cost()
    if cost:
        lines.append(f"cost: ${cost:.4f}")
    return CommandResult(text="\n".join(lines))


@register(
    "/compact", "summarise old history to free context (/compact [focus])", "mutate", agent=True
)
async def _compact(env, arg):
    compactor = env.agent.compactor
    if compactor is None:
        return CommandResult(notices=[Notice("error", "/compact is not available here")])
    notices = []
    try:
        async for event in compactor.compact(env.agent, env.session, instructions=arg):
            if isinstance(event, Compacted):
                notices.append(Notice("info", f"compacted {event.removed} messages into a summary"))
            elif hasattr(event, "level"):
                notices.append(Notice(event.level, event.text))
    except Exception as exc:  # noqa: BLE001 - a failed compact must not kill the session
        return CommandResult(notices=[Notice("error", f"/compact failed: {exc}")])
    return CommandResult(notices=notices or [Notice("dim", "nothing to compact yet")])


@register("/diff", "show file changes made this session", "read")
async def _diff(env, arg):
    journal = env.session.undo_journal
    text = await asyncio.to_thread(journal.diff) if journal is not None else ""
    if not text:
        return CommandResult(notices=[Notice("dim", "no changes this session")])
    fence = "`" * 3
    return CommandResult(text=f"{fence}diff\n{text}\n{fence}")


def _last_user_line(session) -> str:
    """First line of the last visible turn's user message, for the confirm question."""
    turns = session.turn_ids()
    if not turns:
        return ""
    for entry in session.entries():
        if entry.turn_id == turns[-1] and entry.message.role == "user":
            text = entry.message.text.strip()
            return text.splitlines()[0][:60] if text else ""
    return ""


@register("/undo", "revert the last turn (files + conversation)", "mutate")
async def _undo(env, arg):
    if not env.session.turn_ids():
        return CommandResult(notices=[Notice("dim", "nothing to undo")])
    if arg.strip() != "--yes":
        what = _last_user_line(env.session)
        question = (
            f"undo the last turn «{what}» (files + conversation)?"
            if what
            else ("undo the last turn (files + conversation)?")
        )
        return CommandResult(confirm=Confirm(question, "/undo --yes", "undo cancelled"))
    result = await asyncio.to_thread(env.session.undo)
    if result is None:
        return CommandResult(notices=[Notice("dim", "nothing to undo")])
    return CommandResult(
        notices=[Notice("info", f"undid the turn: {result.note}")],
        effects={"undo_depth": env.session.undo_depth},
    )


@register("/redo", "re-apply the last undone turn", "mutate")
async def _redo(env, arg):
    result = await asyncio.to_thread(env.session.redo)
    if result is None:
        return CommandResult(notices=[Notice("dim", "nothing to redo")])
    return CommandResult(
        notices=[Notice("info", f"redo: {result.note}")],
        effects={"undo_depth": env.session.undo_depth},
    )


@register("/rewind", "undo the last N turns (/rewind 3)", "mutate")
async def _rewind(env, arg):
    parts = arg.split()
    count = next((int(p) for p in parts if p.isdigit()), 1)
    available = len(env.session.turn_ids())
    if not available or count < 1:
        return CommandResult(notices=[Notice("dim", "nothing to rewind")])
    count = min(count, available)
    if "--yes" not in parts:
        return CommandResult(
            confirm=Confirm(
                f"rewind the last {count} turn(s) (files + conversation)?",
                f"/rewind {count} --yes",
                "rewind cancelled",
            )
        )
    for _ in range(count):
        await asyncio.to_thread(env.session.undo)
    return CommandResult(
        notices=[Notice("info", f"rewound {count} turn(s)")],
        effects={"undo_depth": env.session.undo_depth},
    )


_EXPORT_FORMATS = ("md", "html", "json")


@register("/export", "save the transcript (/export [md|html|json] [path])", "read", agent=True)
async def _export(env, arg):
    import time

    parts = arg.split()
    fmt = next((p for p in parts if p in _EXPORT_FORMATS), "md")
    rest = [p for p in parts if p not in _EXPORT_FORMATS]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rel = rest[0] if rest else f".luna/exports/{env.session.id[:8]}-{stamp}.{fmt}"
    workspace = env.agent.workspace
    try:
        target = workspace.resolve(rel)
        text = await asyncio.to_thread(env.session.export, fmt)

        def write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

        await asyncio.to_thread(write)
        shown = workspace.rel(target)
    except (LunaCoreError, OSError) as exc:
        return CommandResult(notices=[Notice("error", f"/export failed: {exc}")])
    return CommandResult(notices=[Notice("info", f"exported to {shown}")], effects={"path": shown})


@register("/btw", "ask a side question without adding it to the conversation", "read", agent=True)
async def _btw(env, arg):
    if not arg:
        return CommandResult(notices=[Notice("dim", "usage: /btw <question>")])
    try:
        answer = await env.agent.side_question(env.session, arg)
    except ProviderError as exc:
        return CommandResult(
            notices=[Notice("error", readable_error(exc, env.agent.provider.name))]
        )
    except LunaCoreError as exc:
        return CommandResult(notices=[Notice("error", f"/btw failed: {exc}")])
    notices = [Notice("warn", answer.notice)] if answer.notice else []
    return CommandResult(text=answer.text, notices=notices)


@register("/steer", "add guidance to the running turn (read at the next step)", "read")
def _steer(env, arg):
    if not arg:
        return CommandResult(notices=[Notice("dim", "usage: /steer <text>")])
    env.session.queue(arg)
    return CommandResult(notices=[Notice("dim", "queued — the agent reads it at the next step")])


@register("/fork", "continue in a new session that copies this one", "mutate")
async def _fork(env, arg):
    child = await asyncio.to_thread(env.session.fork)
    return CommandResult(
        notices=[Notice("info", f"forked into {child.id[:8]}")], effects={"session": child.id}
    )


register_ui("/background", "run a prompt in the background (/background <prompt>)")


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
async def _verify(env, arg):
    if not env.config.verify_command:
        return CommandResult(notices=[Notice("dim", "set agent.verify_command in config first")])
    ok, tail = await asyncio.to_thread(run_verify, env.config.verify_command, env.workdir)
    return CommandResult(
        notices=[Notice("ok", "✓ verify ok") if ok else Notice("warn", f"verify failed\n{tail}")]
    )


@register("/diagnose", "run the project's diagnostics command now", "mutate")
async def _diagnose(env, arg):
    from luna.turn import diagnose

    cmd = env.config.diagnose_command
    if cmd == "auto":
        cmd = diagnose.detect(env.workdir)
    if not cmd:
        return CommandResult(notices=[Notice("dim", "no diagnose command configured or detected")])
    text = await asyncio.to_thread(diagnose.run, cmd, env.workdir, [])
    return (
        CommandResult(text=text) if text else CommandResult(notices=[Notice("dim", "no findings")])
    )


@register("/init", "generate or update AGENTS.md", "prompt")
def _init(env, arg):
    # The client sends the prompt with reload_after=True: the flag travels with
    # the message, so a send that never happens leaves no stale reload behind.
    return CommandResult(prompt=init_prompt(env.workdir), effects={"reload_after_turn": True})


@register("/model", "pick a model interactively, or /model <name> to switch directly", "mutate")
async def _model(env, arg):
    if not arg:
        current = Notice("dim", f"model: {env.config.model or '(provider default)'}")
        models = await asyncio.to_thread(_models_for, env.config)
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
        await env.switch_model(arg)
    except Exception as exc:  # noqa: BLE001 - a bad model must not kill the session
        return CommandResult(notices=[Notice("error", f"could not switch: {exc}")])
    return CommandResult(notices=[Notice("info", f"model → {arg}")], effects={"model": arg})


@register("/provider", "show or switch the provider (/provider <key>)", "mutate")
async def _provider(env, arg):
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
        await env.switch_provider(arg)
    except Exception as exc:  # noqa: BLE001 - a bad provider must not kill the session
        return CommandResult(notices=[Notice("error", f"could not switch: {exc}")])
    return CommandResult(notices=[Notice("info", f"provider → {arg}")], effects={"provider": arg})


@register("/reload", "rebuild the agent with the current config", "mutate")
async def _reload(env, arg):
    if not env.can_rebuild:
        return CommandResult(notices=[Notice("error", "/reload is not available here")])
    try:
        await env.rebuild()
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
# Every client exits by itself; registered so typing "/e" finds it. The /quit
# alias still works everywhere but is not listed (its help says "also /quit").
register_ui("/exit", "leave Luna (also /quit, Ctrl-D)")
