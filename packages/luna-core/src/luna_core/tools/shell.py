"""The ``execute`` tool: run a shell command in the session sandbox."""

from __future__ import annotations

from luna_core.messages import Text
from luna_core.tools.base import ToolContext, ToolOutput, tool

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600


@tool(mutating=True)
async def execute(ctx: ToolContext, command: str, timeout: int = DEFAULT_TIMEOUT) -> ToolOutput:
    """Run a shell command in the project directory.

    Args:
        ctx: Tool context (hidden from the schema).
        command: Shell command line.
        timeout: Seconds before the command is killed (positive, max 600; the default
            is 120). Zero or negative values are rejected, not replaced by the default.

    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    if int(timeout) <= 0:
        return ToolOutput.error(f"timeout must be a positive number of seconds, got {timeout}")
    seconds = min(int(timeout), MAX_TIMEOUT)
    result = await ctx.sandbox.run(
        command, cwd=ctx.workspace.root, timeout=seconds, cancel=ctx.cancel
    )
    text = f"{result.output.rstrip()}\n[exit code {result.exit_code}]".lstrip()
    failed = result.exit_code != 0 or result.timed_out or result.cancelled
    first = command.strip().splitlines()[0][:60] if command.strip() else ""
    if failed:
        return ToolOutput((Text(text),), True, f"exit {result.exit_code}: {first}")
    return ToolOutput.text(text, f"ran {first}")
