"""Slash commands shared by every Luna surface (REPL, server, TUI)."""

from luna.commands import builtin  # noqa: F401 - registers the built-ins
from luna.commands.base import HISTORY_COMMANDS, Choice, Command, CommandEnv, CommandResult, Confirm
from luna.commands.registry import REGISTRY, command_kind, list_commands, needs_agent, run_line

__all__ = [
    "HISTORY_COMMANDS",
    "REGISTRY",
    "Choice",
    "Command",
    "CommandEnv",
    "CommandResult",
    "Confirm",
    "command_kind",
    "list_commands",
    "needs_agent",
    "run_line",
]
