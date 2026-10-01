"""The single slash-command registry shared by the REPL, the server and the TUI."""

from __future__ import annotations

from luna.commands.base import Command, CommandResult
from luna.turn.engine import Notice

REGISTRY: dict[str, Command] = {}


def register(name: str, help: str, kind: str):
    """Register ``fn(env, arg) -> CommandResult`` as the command ``name``."""

    def deco(fn):
        REGISTRY[name] = Command(name, help, kind, fn)
        return fn

    return deco


def register_ui(name: str, help: str) -> None:
    """Register a client-side command: listed for help/autocomplete, never run here."""
    REGISTRY[name] = Command(name, help, "ui", None)


def command_kind(name: str, user_commands: dict) -> str:
    """Kind of ``name``; user commands are prompts; unknown counts as mutate."""
    if name in REGISTRY:
        return REGISTRY[name].kind
    return "prompt" if name[1:] in user_commands else "mutate"


def run_line(line: str, env) -> CommandResult:
    """Run one ``/command args`` line against ``env``."""
    name, _, arg = line.strip().partition(" ")
    arg = arg.strip()
    command = REGISTRY.get(name)
    if command is not None and command.run is not None:
        return command.run(env, arg)
    bare = name[1:]
    if command is None and bare in (env.user_commands or {}):
        from luna.repl.usercmd import expand

        return CommandResult(prompt=expand(env.user_commands[bare], arg, env.workdir))
    return CommandResult(notices=[Notice("error", f"неизвестная команда {name} — /help")])


def list_commands(user_commands: dict) -> list[dict]:
    """Built-in + user commands as ``[{name, help, kind}]`` for help/autocomplete."""
    out = [{"name": c.name, "help": c.help, "kind": c.kind} for c in REGISTRY.values()]
    for name, cmd in sorted((user_commands or {}).items()):
        if f"/{name}" not in REGISTRY:
            out.append({"name": f"/{name}", "help": cmd.description, "kind": "prompt"})
    return out
