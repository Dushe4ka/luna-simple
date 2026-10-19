"""The single slash-command registry shared by the REPL, the server and the TUI."""

from __future__ import annotations

import inspect

from luna.commands.base import Command, CommandResult
from luna.turn.engine import Notice

REGISTRY: dict[str, Command] = {}


def register(name: str, help: str, kind: str, *, agent: bool = False):
    """Register ``fn(env, arg) -> CommandResult`` (sync or async) as the command ``name``.

    ``agent=True`` marks a handler that reads ``env.agent``.
    """

    def deco(fn):
        REGISTRY[name] = Command(name, help, kind, fn, agent)
        return fn

    return deco


def register_ui(name: str, help: str) -> None:
    """Register a client-side command: listed for help/autocomplete, never run here."""
    REGISTRY[name] = Command(name, help, "ui", None)


def needs_agent(name: str) -> bool:
    """Whether the built-in command ``name`` reads ``env.agent``."""
    command = REGISTRY.get(name)
    return command is not None and command.agent


def command_kind(name: str, user_commands: dict) -> str:
    """Kind of ``name``; user commands are prompts; unknown counts as mutate."""
    if name in REGISTRY:
        return REGISTRY[name].kind
    return "prompt" if name[1:] in user_commands else "mutate"


async def run_line(line: str, env) -> CommandResult:
    """Run one ``/command args`` line against ``env`` (handlers may be sync or async).

    A handler that raises is reported as an error notice: a command never
    takes the REPL or the server down.
    """
    name, _, arg = line.strip().partition(" ")
    arg = arg.strip()
    command = REGISTRY.get(name)
    if command is not None and command.run is not None:
        try:
            result = command.run(env, arg)
            return await result if inspect.isawaitable(result) else result
        except Exception as exc:  # noqa: BLE001 - one bad command must not kill the session
            return CommandResult(notices=[Notice("error", f"{name} failed: {exc}")])
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
