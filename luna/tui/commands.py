"""Slash-command filtering for the TUI's input autocomplete."""

from __future__ import annotations

from luna.repl.commands import HELP

#: Used until the server's list arrives (or if it cannot be fetched).
FALLBACK_COMMANDS = [{"name": n, "help": h, "kind": "mutate"} for n, h in HELP.items()]
#: Commands the server knows that the REPL's ``HELP`` table does not list.
_EXTRA = {
    "/background": ("run a prompt in the background (/background <prompt>)", "ui"),
    "/btw": ("ask a side question without adding it to the conversation", "read"),
    "/steer": ("add guidance to the running turn (read at the next step)", "read"),
    "/rewind": ("undo the last N turns (/rewind 3)", "mutate"),
    "/export": ("save the transcript (/export [md|html|json] [path] [--force])", "mutate"),
    "/fork": ("continue in a new session that copies this one", "mutate"),
}
FALLBACK_COMMANDS += [
    {"name": n, "help": h, "kind": k}
    for n, (h, k) in _EXTRA.items()
    if all(c["name"] != n for c in FALLBACK_COMMANDS)
]


def filter_commands(prefix: str, commands: list[dict] | None = None) -> list[tuple[str, str]]:
    """Return (name, help) pairs whose name starts with ``prefix``."""
    source = commands if commands is not None else FALLBACK_COMMANDS
    return [(c["name"], c["help"]) for c in source if c["name"].startswith(prefix)]
