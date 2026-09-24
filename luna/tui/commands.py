"""Slash-command filtering for the TUI's input autocomplete."""

from __future__ import annotations

from luna.repl.commands import HELP


def filter_commands(prefix: str) -> list[tuple[str, str]]:
    """Return (name, help_text) pairs whose name starts with ``prefix``."""
    return [(name, text) for name, text in HELP.items() if name.startswith(prefix)]
