"""Slash commands shared by every Luna surface (REPL, server, TUI)."""

from luna.commands import builtin  # noqa: F401 - registers the built-ins
from luna.commands.base import Choice, Command, CommandEnv, CommandResult, Confirm
from luna.commands.registry import REGISTRY, command_kind, list_commands, run_line

__all__ = [
    "REGISTRY",
    "Choice",
    "Command",
    "CommandEnv",
    "CommandResult",
    "Confirm",
    "command_kind",
    "list_commands",
    "run_line",
]
