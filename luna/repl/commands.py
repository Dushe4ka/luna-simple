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

import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass

from luna_core.errors import LunaCoreError
from luna_core.session import Session
from rich.console import Console

from luna.commands import REGISTRY, run_line
from luna.config.config import LunaConfig
from luna.config.credentials import get_api_key
from luna.config.providers import merge_providers
from luna.core.session_state import SessionState, SessionStateStore
from luna.core.store import conversations
from luna.repl import usercmd
from luna.repl.setup_wizard import choose_model
from luna.ui.interact import arrow_confirm, arrow_pick, in_thread
from luna.ui.theme import PALETTE

#: Built from the shared registry: one source for help text.
HELP: dict[str, str] = {c.name: c.help for c in REGISTRY.values()}


@dataclass
class CommandContext:
    """Everything a slash-command handler might need.

    ``bundle`` and ``session``/``state`` are replaced in place by ``/reload``,
    ``/model``, ``/new`` and ``/resume``; ``rebuild`` is an async callable
    returning a new bundle (``None`` when this surface cannot rebuild).
    """

    console: Console
    config: LunaConfig
    bundle: object
    rebuild: Callable | None
    session: Session
    log: object
    state: SessionState
    input_fn: Callable[[str], str] | None = None
    user_commands: dict | None = None
    plan_state: list | None = None


@dataclass
class DispatchResult:
    """Outcome of a dispatched line.

    ``prompt`` becomes the next turn's message; ``reload_after`` asks for a
    rebuild once that turn finished (``/init`` writes a new AGENTS.md).
    """

    handled: bool = True
    exit: bool = False
    prompt: str | None = None
    reload_after: bool = False


def _workdir(ctx: CommandContext) -> str:
    return ctx.session.workdir


def _session_rows(ctx: CommandContext) -> list:
    """Conversations of this folder, newest first (no children, no empty sessions)."""
    return conversations(ctx.log, _workdir(ctx))


def _help(ctx: CommandContext, arg: str) -> None:
    for name, help_text in HELP.items():
        ctx.console.print(f"  [bold {PALETTE['peri']}]{name}[/]  {help_text}")


def _clear(ctx: CommandContext, arg: str) -> None:
    ctx.console.clear()


def _new(ctx: CommandContext, arg: str) -> None:
    workdir = _workdir(ctx)
    ctx.session = Session.create(ctx.log, workdir)
    ctx.state = SessionState(ctx.session.id, workdir)
    ctx.console.print(f"[{PALETTE['blue']}]started a new session[/]")


def _sessions(ctx: CommandContext, arg: str) -> None:
    """List past sessions for this directory."""
    rows = _session_rows(ctx)
    if not rows:
        ctx.console.print("[dim]no sessions recorded for this directory[/]")
        return
    for n, r in enumerate(rows, 1):
        ctx.console.print(f"  [{n}] {r.title or '(untitled)'}", markup=False)


async def _resume(ctx: CommandContext, arg: str) -> None:
    """Resume a past session: /resume <number> (no arg picks interactively)."""
    rows = _session_rows(ctx)
    if not arg:
        if not rows:
            ctx.console.print("[dim]no sessions recorded for this directory[/]")
            return
        if ctx.input_fn is not None:
            options = [(r.id, r.title or "(untitled)") for r in rows]
            picked = await in_thread(arrow_pick, ctx.console, ctx.input_fn, options, default=None)
            if picked is not None:
                _do_resume(ctx, picked)
                return
        for n, r in enumerate(rows, 1):
            ctx.console.print(f"  [{n}] {r.title or '(untitled)'}", markup=False)
        ctx.console.print("[dim]usage: /resume <number>[/]")
        return
    if arg.isdigit() and 1 <= int(arg) <= len(rows):
        target = rows[int(arg) - 1].id
    else:
        target = arg  # treat as a session id
    _do_resume(ctx, target)


def _do_resume(ctx: CommandContext, target: str) -> None:
    from luna.core.session import print_recap  # lazy: session imports this module

    info = ctx.log.info(target)
    if info is None:
        ctx.console.print(f"no session {target!r}", style=PALETTE["mauve"], markup=False)
        return
    if info.workdir != _workdir(ctx):
        ctx.console.print(f"[{PALETTE['mauve']}]session {target[:8]} belongs to another folder[/]")
        return
    try:
        session = Session.open(ctx.log, target)
    except LunaCoreError as exc:
        ctx.console.print(str(exc), style=PALETTE["mauve"], markup=False)
        return
    ctx.session = session
    ctx.state = SessionStateStore().load(session.id, session.workdir)
    print_recap(session, ctx.console)
    ctx.console.print(f"[{PALETTE['blue']}]resumed session {target[:8]}[/]")


def _init(ctx: CommandContext, arg: str) -> DispatchResult:
    """Generate or update AGENTS.md for this repo (a turn, then a rebuild)."""
    from luna.extensions.initgen import init_prompt

    return DispatchResult(prompt=init_prompt(_workdir(ctx)), reload_after=True)


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


async def swap_bundle(ctx: CommandContext) -> None:
    """Build a new bundle; on success it replaces (and closes) the old one.

    Custom ``.luna/commands/*.md`` are re-read too, like everything else a
    reload picks up.
    """
    new = await ctx.rebuild()
    old, ctx.bundle = ctx.bundle, new
    if old is not None and old is not new:
        await old.aclose()
    ctx.user_commands = usercmd.load(_workdir(ctx))


class _ReplEnv:
    """:class:`luna.commands.CommandEnv` over the REPL's mutable context."""

    manager = None

    def __init__(self, ctx: CommandContext) -> None:
        self._ctx = ctx
        self.session = ctx.session
        self.state = ctx.state
        self.workdir = ctx.session.workdir
        self.pinned = ctx.state.pinned
        self.user_commands = ctx.user_commands or {}

    @property
    def config(self) -> LunaConfig:
        return self._ctx.config

    @property
    def bundle(self):
        return self._ctx.bundle

    @property
    def agent(self):
        return self._ctx.bundle.agent

    @property
    def can_rebuild(self) -> bool:
        return self._ctx.rebuild is not None

    def live_tasks(self) -> bool:
        bundle = self._ctx.bundle
        background = bundle.agent.services.get("background") if bundle is not None else None
        if background is None:
            return False
        tasks = background.list(self.session.id)
        return any(t.status in ("queued", "running") for t in tasks)

    def get_plan(self):
        return None if self._ctx.plan_state is None else self._ctx.plan_state[0]

    def set_plan(self, on: bool) -> None:
        self._ctx.plan_state[0] = on

    async def rebuild(self) -> None:
        if self._ctx.rebuild is None:
            raise RuntimeError("not available here")
        await swap_bundle(self._ctx)

    async def _switch(self, provider, model) -> None:
        cfg = self._ctx.config
        previous = (cfg.provider, cfg.model)
        cfg.provider, cfg.model = provider, model
        try:
            await self.rebuild()
        except Exception:
            cfg.provider, cfg.model = previous
            raise

    async def switch_model(self, model: str) -> None:
        await self._switch(self._ctx.config.provider, model)

    async def switch_provider(self, provider: str) -> None:
        await self._switch(provider, None)


async def _render(result, ctx: CommandContext) -> DispatchResult:
    for notice in result.notices:
        ctx.console.print(notice.text, style=_STYLE.get(notice.level, ""), markup=False)
    if result.text:
        ctx.console.print(result.text, markup=False)
    out = DispatchResult()
    if result.choice is not None and ctx.input_fn is not None:
        picked = await in_thread(
            arrow_pick, ctx.console, ctx.input_fn, result.choice.options, default=None
        )
        if picked is not None:
            return await dispatch(result.choice.resubmit.format(value=picked), ctx)
    if result.confirm is not None:
        confirmed = True
        if ctx.input_fn is not None:
            question = result.confirm.question
            confirmed = await in_thread(arrow_confirm, ctx.console, ctx.input_fn, question)
            if confirmed is None:
                answer = await in_thread(ctx.input_fn, f"{question} [y/N] ")
                confirmed = answer.strip().lower() in ("y", "yes")
        if not confirmed:
            ctx.console.print(f"[dim]{result.confirm.cancelled}[/]")
            return out
        return await dispatch(result.confirm.resubmit, ctx)
    if result.prompt is not None:
        out.prompt = result.prompt
        out.reload_after = bool(result.effects.get("reload_after_turn"))
    return out


async def dispatch(line: str, ctx: CommandContext) -> DispatchResult:
    """Route a REPL line through the shared registry. Non-slash lines return ``handled=False``."""
    if not line.startswith("/"):
        return DispatchResult(handled=False)
    name, _, arg = line.partition(" ")
    arg = arg.strip()
    if name in ("/exit", "/quit"):
        return DispatchResult(exit=True)
    if name in _UI:
        res = _UI[name](ctx, arg)
        if inspect.isawaitable(res):
            res = await res
        return res or DispatchResult()
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
        arg = await in_thread(
            choose_model, ctx.console, ctx.input_fn, ctx.config.provider, spec, api_key=api_key
        )
        line = f"/model {arg}"
    return await _render(await run_line(line, _ReplEnv(ctx)), ctx)
