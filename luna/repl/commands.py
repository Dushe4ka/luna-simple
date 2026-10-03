"""REPL adapter over the shared slash-command registry (:mod:`luna.commands`).

``run_repl`` in :mod:`luna.core.session` delegates every ``/command`` line to
:func:`dispatch`. Commands run through the same registry the server uses and
return a structured result, which is rendered here to the console; a
``choice`` / ``confirm`` is asked with the arrow pickers and answered by
re-dispatching the command. A few commands stay REPL-only (``/help``,
``/clear``, ``/new``, ``/sessions``, ``/resume``, ``/init``, and the
interactive part of ``/model``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console

from luna.commands import REGISTRY, run_line
from luna.config.config import LunaConfig
from luna.config.credentials import get_api_key
from luna.config.providers import merge_providers
from luna.config.usage import SessionUsage
from luna.repl.setup_wizard import choose_model
from luna.turn.context import PinnedFiles
from luna.ui.interact import arrow_confirm, arrow_pick
from luna.ui.theme import PALETTE

#: Built from the shared registry (one source for help text) plus /exit,
#: which every client handles itself.
HELP: dict[str, str] = {
    **{c.name: c.help for c in REGISTRY.values()},
    "/exit": "leave Luna (also /quit, Ctrl-D)",
}


@dataclass
class CommandContext:
    """Everything a slash-command handler might need.

    Fields with defaults are populated by later tasks; adding one here does
    not touch existing call sites.
    """

    console: Console
    config: LunaConfig
    agent: object
    rebuild: Callable[[], object] | None
    thread_id: str
    workdir: str
    index: object | None = None
    session_id: str = ""
    pinned: object | None = None  # context.PinnedFiles, Task 6
    usage: object | None = None  # usage.SessionUsage, Task 5
    permissions: object | None = None  # permissions ruleset, Task 7
    input_fn: Callable[[str], str] | None = None
    user_commands: dict | None = None  # usercmd.load(workdir), Task 5
    plan_state: list | None = None


@dataclass
class DispatchResult:
    """Outcome of a dispatched line."""

    handled: bool = True
    agent: object | None = None
    thread_id: str | None = None
    exit: bool = False
    prompt: str | None = None


def _print_help(console: Console) -> None:
    for name, help_text in HELP.items():
        console.print(f"  [bold {PALETTE['peri']}]{name}[/]  {help_text}")


def _help(ctx: CommandContext, arg: str) -> None:
    _print_help(ctx.console)


def _clear(ctx: CommandContext, arg: str) -> None:
    ctx.console.clear()


def _new(ctx: CommandContext, arg: str) -> DispatchResult:
    tid = uuid.uuid4().hex
    ctx.console.print(f"[{PALETTE['blue']}]started a new thread[/]")
    return DispatchResult(thread_id=tid)


def _sessions(ctx: CommandContext, arg: str) -> None:
    """List past sessions for this directory."""
    if ctx.index is None:
        ctx.console.print("[dim]session history is not available here[/]")
        return
    rows = ctx.index.list(ctx.workdir)
    if not rows:
        ctx.console.print("[dim]no sessions recorded for this directory[/]")
        return
    for n, r in enumerate(rows, 1):
        ctx.console.print(f"  [{n}] {r.title}")


def _resume(ctx: CommandContext, arg: str) -> DispatchResult | None:
    """Resume a past session: /resume <number> (no arg picks interactively)."""
    if ctx.index is None:
        ctx.console.print("[dim]session history is not available here[/]")
        return None
    rows = ctx.index.list(ctx.workdir)
    if not arg:
        if not rows:
            ctx.console.print("[dim]no sessions recorded for this directory[/]")
            return None
        if ctx.input_fn is not None:
            picked = arrow_pick(
                ctx.console,
                ctx.input_fn,
                [(r.thread_id, r.title) for r in rows],
                default=None,
            )
            if picked is not None:
                return _do_resume(ctx, picked)
        for n, r in enumerate(rows, 1):
            ctx.console.print(f"  [{n}] {r.title}")
        ctx.console.print("[dim]usage: /resume <number>[/]")
        return None
    target = None
    if arg.isdigit() and 1 <= int(arg) <= len(rows):
        target = rows[int(arg) - 1].thread_id
    else:
        target = arg  # treat as a thread id
    return _do_resume(ctx, target)


def _do_resume(ctx: CommandContext, target: str) -> DispatchResult:
    from luna.core.session import _print_recap  # lazy: session imports commands

    config = {"configurable": {"thread_id": target}}
    _print_recap(ctx.agent, config, ctx.console)
    ctx.console.print(f"[{PALETTE['blue']}]resumed session {target[:8]}[/]")
    return DispatchResult(thread_id=target)


def _init(ctx: CommandContext, arg: str) -> DispatchResult | None:
    """Generate or update AGENTS.md for this repo."""
    from luna.core.session import run_once  # lazy: session imports commands
    from luna.extensions.initgen import init_prompt

    run_once(
        ctx.agent,
        init_prompt(ctx.workdir),
        thread_id=ctx.thread_id,
        console=ctx.console,
        workdir=ctx.workdir,
    )
    if ctx.rebuild is not None:
        return DispatchResult(agent=ctx.rebuild())
    return None


_UI: dict[str, Callable] = {
    "/help": _help,
    "/clear": _clear,
    "/new": _new,
    "/sessions": _sessions,
    "/resume": _resume,
    "/init": _init,
}

_STYLE = {
    "dim": "dim",
    "ok": "dim",
    "info": PALETTE["blue"],
    "warn": "yellow",
    "error": PALETTE["mauve"],
}


class _ReplEnv:
    """:class:`luna.commands.CommandEnv` over the REPL's mutable context."""

    def __init__(self, ctx: CommandContext) -> None:
        self._ctx = ctx
        self.new_agent = None
        self.workdir = ctx.workdir
        self.thread_id = ctx.thread_id
        self.session_id = ctx.session_id
        self.index = ctx.index
        self.user_commands = ctx.user_commands or {}
        if ctx.pinned is None:
            ctx.pinned = PinnedFiles()
        self.pinned = ctx.pinned
        self.usage = ctx.usage if ctx.usage is not None else SessionUsage()

    @property
    def config(self):
        return self._ctx.config

    @property
    def agent(self):
        return self.new_agent if self.new_agent is not None else self._ctx.agent

    @property
    def can_rebuild(self) -> bool:
        return self._ctx.rebuild is not None

    def get_plan(self):
        return None if self._ctx.plan_state is None else self._ctx.plan_state[0]

    def set_plan(self, on: bool) -> None:
        self._ctx.plan_state[0] = on

    def rebuild(self) -> None:
        if self._ctx.rebuild is None:
            raise RuntimeError("not available here")
        self.new_agent = self._ctx.rebuild()

    def _switch(self, provider, model) -> None:
        cfg = self._ctx.config
        previous = (cfg.provider, cfg.model)
        cfg.provider, cfg.model = provider, model
        try:
            self.rebuild()
        except Exception:
            cfg.provider, cfg.model = previous
            raise

    def switch_model(self, model: str) -> None:
        self._switch(self._ctx.config.provider, model)

    def switch_provider(self, provider: str) -> None:
        self._switch(provider, None)


def _render(result, ctx: CommandContext, env: _ReplEnv) -> DispatchResult:
    for notice in result.notices:
        ctx.console.print(notice.text, style=_STYLE.get(notice.level, ""), markup=False)
    if result.text:
        ctx.console.print(result.text, markup=False)
    out = DispatchResult(agent=env.new_agent)
    if result.choice is not None and ctx.input_fn is not None:
        picked = arrow_pick(ctx.console, ctx.input_fn, result.choice.options, default=None)
        if picked is not None:
            return dispatch(result.choice.resubmit.format(value=picked), ctx)
    if result.confirm is not None:
        confirmed = True
        if ctx.input_fn is not None:
            confirmed = arrow_confirm(ctx.console, ctx.input_fn, result.confirm.question)
            if confirmed is None:
                answer = ctx.input_fn(f"{result.confirm.question} [y/N] ")
                confirmed = answer.strip().lower() in ("y", "yes")
        if not confirmed:
            ctx.console.print(f"[dim]{result.confirm.cancelled}[/]")
            return out
        return dispatch(result.confirm.resubmit, ctx)
    if result.prompt is not None:
        out.prompt = result.prompt
    return out


def dispatch(line: str, ctx: CommandContext) -> DispatchResult:
    """Route a REPL line through the shared registry. Non-slash lines return ``handled=False``."""
    if not line.startswith("/"):
        return DispatchResult(handled=False)
    name, _, arg = line.partition(" ")
    arg = arg.strip()
    if name in ("/exit", "/quit"):
        return DispatchResult(exit=True)
    if name in _UI:
        return _UI[name](ctx, arg) or DispatchResult()
    if name == "/model" and not arg and ctx.input_fn is None:
        # nobody to pick from a list: just say which model is in use (no network)
        ctx.console.print(
            f"model: {ctx.config.model or '(provider default)'}", markup=False, highlight=False
        )
        return DispatchResult()
    if name == "/model" and not arg and ctx.input_fn is not None and ctx.rebuild is not None:
        registry = merge_providers(ctx.config.custom_providers)
        spec = registry[ctx.config.provider]
        api_key = os.environ.get(spec.env_var) if spec.env_var else None
        if not api_key and spec.env_var:
            api_key = get_api_key(ctx.config.provider)
        arg = choose_model(ctx.console, ctx.input_fn, ctx.config.provider, spec, api_key=api_key)
        line = f"/model {arg}"
    env = _ReplEnv(ctx)
    return _render(run_line(line, env), ctx, env)
