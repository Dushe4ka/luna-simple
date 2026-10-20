"""``write_todos``: the agent's visible plan."""

from __future__ import annotations

from luna_core.events import TodosUpdated
from luna_core.tools.base import Tool, ToolContext, ToolOutput

STATUSES = ("pending", "in_progress", "completed")
_SCHEMA = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "description": "The full, updated plan.",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "status": {"type": "string", "enum": list(STATUSES)},
                },
                "required": ["content", "status"],
            },
        }
    },
    "required": ["todos"],
    "additionalProperties": False,
}


async def _write_todos(ctx: ToolContext, args: dict) -> ToolOutput:
    """Validate, store and announce the agent's plan.

    Args:
        ctx: Tool context (hidden from the schema).
        args: Raw arguments; must contain ``todos``.

    """
    todos = args.get("todos")
    if not isinstance(todos, list):
        return ToolOutput.error("todos must be a list")
    clean = []
    for item in todos:
        if not isinstance(item, dict) or not str(item.get("content", "")).strip():
            return ToolOutput.error("each todo needs non-empty content")
        if item.get("status") not in STATUSES:
            return ToolOutput.error(f"status must be one of {', '.join(STATUSES)}")
        clean.append({"content": str(item["content"]).strip(), "status": item["status"]})
    ctx.state["todos"] = clean
    ctx.emit(TodosUpdated(clean))
    done = sum(1 for t in clean if t["status"] == "completed")
    return ToolOutput.text(f"plan updated: {done}/{len(clean)} done", f"todos {done}/{len(clean)}")


write_todos = Tool(
    "write_todos",
    "Keep a short plan for multi-step work; send the whole list each time.",
    _SCHEMA,
    _write_todos,
    raw=True,
)
