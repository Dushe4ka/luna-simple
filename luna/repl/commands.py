"""REPL adapter over the shared slash-command registry (:mod:`luna.commands`).

``run_repl`` in :mod:`luna.core.session` delegates every ``/command`` line to
:func:`dispatch`. Commands run through the same registry the server uses and
return a structured result, which is rendered here to the console; a
``choice`` / ``confirm`` is asked with the arrow pickers and answered by
re-dispatching the command. A few commands stay REPL-only (``/help``,
``/clear``, ``/new``, ``/sessions``, ``/resume``, ``/init``, and the
interactive part of ``/model``).

``/undo``, ``/redo``, ``/diff`` and ``/compact`` act on the luna-core session
here (the shared handlers still drive the server's LangGraph agents).

Registry handlers are still synchronous: they run in a worker thread, so a
handler that rebuilds the agent (``/reload``, ``/model``, ``/provider``)
schedules the async rebuild on the REPL's event loop and waits for it.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass

from luna_core.errors import LunaCoreError
from luna_core.events import Compacted as CoreCompacted
from luna_core.events import Notice as CoreNotice
from luna_core.session import Session
from rich.console import Console

from luna.commands import REGISTRY, CommandResult, Confirm, run_line
from luna.config.config import LunaConfig
from luna.config.credentials import get_api_key
from luna.config.providers import merge_providers
from luna.config.usage import SessionUsage, TurnUsage
from luna.core.session_state import SessionState, SessionStateStore
from luna.core.store import conversations
from luna.repl import usercmd
from luna.repl.setup_wizard import choose_model
from luna.turn.engine import Notice
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


async def _undo(ctx: CommandContext, arg: str) -> CommandResult:
    session = ctx.session
    if not session.undo_depth:
        return CommandResult(notices=[Notice("dim", "nothing to undo")])
    if arg != "--yes":
        return CommandResult(
            confirm=Confirm(
                "undo the last turn (files + conversation)?", "/undo --yes", "undo cancelled"
            )
        )
    result = await asyncio.to_thread(session.undo)
    if result is None:
        return CommandResult(notices=[Notice("dim", "nothing to undo")])
    return CommandResult(notices=[Notice("info", result.note)])


async def _redo(ctx: CommandContext, arg: str) -> CommandResult:
    result = await asyncio.to_thread(ctx.session.redo)
    if result is None:
        return CommandResult(notices=[Notice("dim", "nothing to redo")])
    return CommandResult(notices=[Notice("info", result.note)])


async def _diff(ctx: CommandContext, arg: str) -> CommandResult:
    journal = ctx.session.undo_journal
    text = await asyncio.to_thread(journal.diff) if journal is not None else ""
    if not text:
        return CommandResult(notices=[Notice("dim", "no changes this session")])
    return CommandResult(text=f"```diff\n{text}\n```")


async def _compact(ctx: CommandContext, arg: str) -> CommandResult:
    agent = ctx.bundle.agent
    if agent.compactor is None:
        return CommandResult(notices=[Notice("error", "/compact is not available here")])
    notices: list[Notice] = []
    try:
        async for event in agent.compactor.compact(agent, ctx.session, instructions=arg):
            if isinstance(event, CoreCompacted):
                notices.append(Notice("info", f"compacted {event.removed} messages into a summary"))
            elif isinstance(event, CoreNotice):
                notices.append(Notice(event.level, event.text))
    except Exception as exc:  # noqa: BLE001 - a failed compact must not kill the session
        return CommandResult(notices=[Notice("error", f"/compact failed: {exc}")])
    return CommandResult(notices=notices)


#: Commands that act on the core session in the REPL (see the module docstring).
_CORE: dict[str, Callable] = {
    "/undo": _undo,
    "/redo": _redo,
    "/diff": _diff,
    "/compact": _compact,
}

_STYLE = {
    "dim": "dim",
    "ok": "dim",
    "info": PALETTE["blue"],
    "warn": "yellow",
    "error": PALETTE["mauve"],
}


def _usage_of(session: Session) -> SessionUsage:
    """Per-turn usage from the event log, in the shape the shared ``/usage`` reads."""
    turns = []
    for rec in session.records():
        if rec.type == "turn_done":
            usage = rec.payload.get("usage") or {}
            tokens_in, tokens_out = int(usage.get("input", 0)), int(usage.get("output", 0))
            turns.append(TurnUsage(tokens_in, tokens_out, tokens_in + tokens_out))
    return SessionUsage(turns=turns)


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
    """:class:`luna.commands.CommandEnv` over the REPL's mutable context.

    Runs in a worker thread (see :func:`dispatch`); ``loop`` is the REPL's
    event loop, where the async rebuild is scheduled.
    """

    def __init__(self, ctx: CommandContext, loop: asyncio.AbstractEventLoop) -> None:
        self._ctx = ctx
        self._loop = loop
        self.session = ctx.session
        self.state = ctx.state
        self.workdir = ctx.session.workdir
        self.pinned = ctx.state.pinned
        self.user_commands = ctx.user_commands or {}
        # what the not-yet-ported shared handlers still read
        self.thread_id = self.session_id = ctx.session.id
        self.index = None
        self.usage = _usage_of(ctx.session)

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

    def get_plan(self):
        return None if self._ctx.plan_state is None else self._ctx.plan_state[0]

    def set_plan(self, on: bool) -> None:
        self._ctx.plan_state[0] = on

    def rebuild(self) -> None:
        if self._ctx.rebuild is None:
            raise RuntimeError("not available here")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:  # waiting on our own loop would deadlock
            raise RuntimeError("rebuild() must be called from a worker thread, not the event loop")
        asyncio.run_coroutine_threadsafe(swap_bundle(self._ctx), self._loop).result()

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
    if name in _CORE:
        return await _render(await _CORE[name](ctx, arg), ctx)
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
    env = _ReplEnv(ctx, asyncio.get_running_loop())
    result = await in_thread(run_line, line, env)
    if inspect.isawaitable(result):  # run_line becomes async with the command port
        result = await result
    return await _render(result, ctx)
