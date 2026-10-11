"""``task``, ``task_status`` and ``task_cancel`` tools."""

from __future__ import annotations

from luna_core.messages import Text
from luna_core.subagents import ISOLATION, SubagentRunner
from luna_core.tools.base import Tool, ToolContext, ToolOutput


def task_tool(runner: SubagentRunner) -> Tool:
    """Build the ``task`` tool bound to ``runner``."""

    async def run(ctx: ToolContext, args: dict) -> ToolOutput:
        parent = ctx.services["agent"]
        session = ctx.services["session"]
        name = str(args.get("subagent_type") or "general-purpose")
        description = str(args.get("description", "")).strip()
        if not description:
            return ToolOutput.error("description is required")
        isolation = str(args.get("isolation") or "auto")
        if args.get("background"):
            background = ctx.services.get("background")
            if background is None:
                return ToolOutput.error("background tasks are not enabled in this host")
            handle = background.start(
                f"{name}: {description[:60]}",
                lambda: runner.run(
                    parent, session, name, description, isolation=isolation, emit=session.notify
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
            name,
            description,
            isolation=isolation,
            emit=ctx.emit,
            cancel=ctx.cancel,
            turn_id=ctx.services.get("turn_id"),
        )
        return ToolOutput(
            (Text(result.render()),), not result.ok, f"{name}: {'done' if result.ok else 'failed'}"
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
        },
        "required": ["description"],
        "additionalProperties": False,
    }
    description = (
        "Delegate a self-contained task to a subagent. "
        "Several task calls in one message run in parallel.\n"
        f"Subagents:\n{runner.describe()}"
    )
    return Tool("task", description, schema, run, raw=True)
