"""``task``, ``task_status`` and ``task_cancel`` tools."""

from __future__ import annotations

from luna_core.dynamic import SubagentRequestError, resolve_spec
from luna_core.messages import Text
from luna_core.roles import ROLES
from luna_core.subagents import ISOLATION, SubagentRunner, SubagentSpec
from luna_core.tools.base import Tool, ToolContext, ToolOutput


def task_tool(runner: SubagentRunner) -> Tool:
    """Build the ``task`` tool bound to ``runner``."""

    def _assemble(parent, args: dict) -> tuple[SubagentSpec, bool]:
        name = str(args.get("subagent_type") or "general-purpose")
        base = runner.specs.get(name)
        if base is None:
            raise SubagentRequestError(
                f"unknown subagent_type {name!r}; available: {', '.join(runner.specs)}"
            )
        skills = runner.skill_names() if runner.skill_names else ()
        return resolve_spec(
            base, args, parent_tools=parent.tools, skills=skills, settings=runner.dynamic
        )

    def _detail(spec: SubagentSpec, dynamic: bool, ok: bool) -> str:
        status = "done" if ok else "failed"
        if not dynamic:
            return f"{spec.name}: {status}"
        return f"{spec.name} (dyn): {status} · {', '.join(spec.tools)} · {spec.model or 'fast'}"

    async def run(ctx: ToolContext, args: dict) -> ToolOutput:
        parent = ctx.services["agent"]
        session = ctx.services["session"]
        description = str(args.get("description", "")).strip()
        if not description:
            return ToolOutput.error("description is required")
        resume = str(args.get("resume") or "") or None
        try:
            if resume:
                # The runner takes the assembly from the stored session; this spec
                # only labels a background task.
                spec = runner.specs.get("general-purpose") or next(iter(runner.specs.values()))
                dynamic = False
            else:
                spec, dynamic = _assemble(parent, args)
        except SubagentRequestError as exc:
            return ToolOutput.error(str(exc))
        isolation = str(args.get("isolation") or "auto")
        if args.get("background"):
            background = ctx.services.get("background")
            if background is None:
                return ToolOutput.error("background tasks are not enabled in this host")
            handle = background.start(
                f"{spec.name}: {description[:60]}",
                lambda: runner.run(
                    parent,
                    session,
                    spec,
                    description,
                    isolation=isolation,
                    emit=session.notify,
                    dynamic=dynamic,
                    resume=resume,
                ),
                session=session,
            )
            return ToolOutput.text(
                f"started background task {handle}; its result arrives in a later message. "
                "Check it with task_status.",
                f"background {handle}",
            )
        result = await runner.run(
            parent,
            session,
            spec,
            description,
            isolation=isolation,
            emit=ctx.emit,
            cancel=ctx.cancel,
            turn_id=ctx.services.get("turn_id"),
            dynamic=dynamic,
            resume=resume,
        )
        return ToolOutput(
            (Text(result.render()),),
            not result.ok,
            _detail(result.spec or spec, result.dynamic, result.ok),
        )

    schema = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "The complete task for the subagent, with all context it needs.",
            },
            "subagent_type": {
                "type": "string",
                "enum": list(runner.specs),
                "default": "general-purpose",
            },
            "background": {
                "type": "boolean",
                "default": False,
                "description": "Return at once; the result arrives later.",
            },
            "isolation": {
                "type": "string",
                "enum": list(ISOLATION),
                "default": "auto",
                "description": "worktree: work on a separate git branch.",
            },
            "prompt": {
                "type": "string",
                "description": "System prompt for this subagent; replaces the profile's.",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Subset of YOUR tools to give it (MCP and LSP tools too). "
                "Never task/manage_*/save_skill.",
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Installed skills whose instructions go into its prompt.",
            },
            "model": {
                "type": "string",
                "description": "A model role or an allowed model id.",
            },
            "max_steps": {"type": "integer", "minimum": 1},
            "context": {
                "type": "string",
                "description": "Facts it needs (paths, decisions); added to its prompt.",
            },
            "resume": {
                "type": "string",
                "description": "Session id from an earlier result: continue that subagent "
                "with its original assembly and isolation; isolation and the other fields "
                "are ignored (background still applies).",
            },
        },
        "required": ["description"],
        "additionalProperties": False,
    }
    settings = runner.dynamic
    if not settings.enabled:
        dynamic_note = "Only subagent_type is available (dynamic subagents are disabled)."
    else:
        models = ", ".join([*ROLES, *settings.models])
        access = (
            "Tools that change files or run commands are allowed."
            if settings.allow_write
            else "Assembled subagents are read-only: no tools that change files or run commands."
        )
        dynamic_note = (
            "You may assemble a subagent for the job: prompt, tools (a subset of yours), "
            f"skills, model ({models}), max_steps (<= {settings.max_steps}), context. {access}"
        )
    description = (
        "Delegate a self-contained task to a subagent. "
        "Several task calls in one message run in parallel.\n"
        f"Subagents:\n{runner.describe()}\n{dynamic_note}"
    )
    built = Tool("task", description, schema, run, raw=True)
    built.runner = runner  # hosts and tests inspect the bound runner
    return built


def task_status_tool() -> Tool:
    """``task_status``: list or inspect background tasks of this session."""

    async def run(ctx: ToolContext, args: dict) -> ToolOutput:
        background = ctx.services.get("background")
        if background is None:
            return ToolOutput.error("background tasks are not enabled in this host")
        handle = str(args.get("handle") or "")
        infos = [background.get(handle)] if handle else background.list(ctx.session_id)
        # A shared host runs many sessions: never show another session's task.
        infos = [i for i in infos if i is not None and i.session_id == ctx.session_id]
        if not infos:
            return ToolOutput.text("no background tasks" if not handle else f"no task {handle}")
        lines = [
            f"{i.handle} · {i.label} · {i.status}" + (f"\n{i.summary[:1500]}" if i.summary else "")
            for i in infos
        ]
        return ToolOutput.text("\n\n".join(lines), f"{len(infos)} tasks")

    schema = {
        "type": "object",
        "properties": {"handle": {"type": "string", "description": "Empty for all tasks."}},
        "additionalProperties": False,
    }
    return Tool("task_status", "Show background tasks and their results.", schema, run, raw=True)


def task_cancel_tool() -> Tool:
    """``task_cancel``: stop a background task."""

    async def run(ctx: ToolContext, args: dict) -> ToolOutput:
        background = ctx.services.get("background")
        if background is None:
            return ToolOutput.error("background tasks are not enabled in this host")
        handle = str(args.get("handle", ""))
        info = background.get(handle)
        if info is not None and info.session_id == ctx.session_id and background.cancel(handle):
            return ToolOutput.text(f"cancelled {handle}")
        return ToolOutput.error(f"{handle} is not running")

    schema = {
        "type": "object",
        "properties": {"handle": {"type": "string"}},
        "required": ["handle"],
        "additionalProperties": False,
    }
    return Tool("task_cancel", "Cancel a running background task.", schema, run, raw=True)
