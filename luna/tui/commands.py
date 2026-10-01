"""Slash-command filtering for the TUI's input autocomplete."""

from __future__ import annotations

from luna.repl.commands import HELP

#: Used until the server's list arrives (or if it cannot be fetched).
FALLBACK_COMMANDS = [{"name": n, "help": h, "kind": "mutate"} for n, h in HELP.items()]


def filter_commands(prefix: str, commands: list[dict] | None = None) -> list[tuple[str, str]]:
    """Return (name, help) pairs whose name starts with ``prefix``."""
    source = commands if commands is not None else FALLBACK_COMMANDS
    return [(c["name"], c["help"]) for c in source if c["name"].startswith(prefix)]
