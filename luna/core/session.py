"""Console surfaces on luna-core: the line REPL and one-shot ``luna -p``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass, field

from luna_core.errors import LunaCoreError
from luna_core.events import (
    Cancelled,
    Compacted,
    NeedsApproval,
    Notice,
    ReloadRequested,
    TextDelta,
    TodosUpdated,
    ToolFinished,
    ToolStarted,
    TurnDone,
    TurnError,
)
from luna_core.policy import Decision
from luna_core.session import Session
from luna_core.usage import Usage
from rich.console import Console

from luna.config.config import LunaConfig
from luna.core.session_state import SessionState, SessionStateStore
from luna.core.store import make_title
from luna.extensions.subagents import subagent_summaries
from luna.repl import usercmd
from luna.repl.commands import HELP as SLASH_COMMANDS
from luna.repl.commands import CommandContext, dispatch, swap_bundle
from luna.turn import engine
from luna.ui.answer import AnswerRenderer
from luna.ui.approve import prompt_decision
from luna.ui.interact import in_thread
from luna.ui.progress import ToolProgress
from luna.ui.theme import PALETTE
from luna.ui.turn import close_turn, open_turn

__all__ = ["SLASH_COMMANDS", "TurnSummary", "render_turn", "run_once", "run_repl"]

_NOTICE_STYLE = {
    "dim": "dim",
    "ok": "dim",
    "info": PALETTE["blue"],
    "warn": "yellow",
    "error": PALETTE["mauve"],
}
_TODO_MARKS = {"completed": "✓", "in_progress": "▸", "pending": "·"}


@dataclass
class TurnSummary:
    """What a rendered turn produced."""

    text: str = ""
    usage: Usage = field(default_factory=Usage)
    cost: float | None = None
    tools: set[str] = field(default_factory=set)
    reload: bool = False
    error: str = ""
    cancelled: bool = False


def _request_dict(request) -> dict:
    """Convert the approval request to the dict :func:`prompt_decision` reads."""
    return {
        "name": request.name,
        "action": request.name,
        "args": request.args,
        "description": request.description,
        "suggested_rule": request.suggested_rule,
        "suggested_rules": list(request.suggested_rules),
    }


@contextlib.contextmanager
def _ctrl_c_cancels(on_sigint: Callable[[], None]):
    """While the block runs, Ctrl-C calls ``on_sigint`` instead of killing the REPL.

    The previous SIGINT handler (e.g. ``asyncio.run``'s) is restored afterwards.
    """
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, on_sigint)
        installed = True
    except (NotImplementedError, RuntimeError, ValueError):
        pass  # not the main thread, or a platform without loop signal handlers
    try:
        yield
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)
            with contextlib.suppress(TypeError, ValueError):
                signal.signal(signal.SIGINT, previous)


async def render_turn(
    agent,
    session: Session,
    text: str,
    *,
    console: Console,
    input_fn: Callable[[str], str],
    images=(),
) -> TurnSummary:
    """Run one turn, rendering it to ``console``; Ctrl-C cancels the turn."""
    summary = TurnSummary()
    parts: list[str] = []
    progress = ToolProgress(console)
    answer = AnswerRenderer(console)
    calls = _Calls()

    def on_sigint() -> None:
        session.cancel_turn()
        if calls.prompt_open and not calls.interrupted:
            # the prompt's thread keeps blocking on stdin: say so now, and drop
            # whatever it answers later (the call must not run)
            calls.interrupted = True
            answer.stop()
            console.print("\nturn cancelled", style=PALETTE["mauve"])
            calls.announced = True

    open_turn(console)
    try:
        with _ctrl_c_cancels(on_sigint):
            run = agent.run(session, text, images=images)
            async with contextlib.aclosing(run) as events:
                async for event in events:
                    _render_event(event, summary, parts, console, progress, answer, calls)
                    if isinstance(event, NeedsApproval):
                        progress.pause()
                        console.print()
                        calls.prompt_open, calls.interrupted = True, False
                        try:
                            raw = await in_thread(
                                prompt_decision,
                                console,
                                _request_dict(event.request),
                                input_fn=input_fn,
                            )
                        except (EOFError, KeyboardInterrupt):
                            session.cancel_turn()  # nobody left to answer: stop the turn
                            continue
                        finally:
                            calls.prompt_open = False
                        if calls.interrupted:
                            continue  # Ctrl-C while asking: never deliver the late answer
                        # the id may be "<child session>:<call id>" — pass it through
                        try:
                            session.decide(event.request.call_id, Decision.from_dict(raw))
                        except LunaCoreError as exc:
                            console.print(
                                f"approval not delivered: {exc}",
                                style=PALETTE["mauve"],
                                markup=False,
                            )
                        progress.resume()
    finally:
        answer.stop()
        progress.close()
    close_turn(console)
    summary.text = "".join(parts).strip()
    return summary


@dataclass
class _Calls:
    """Per-turn render state: tool calls seen, approval prompt and Ctrl-C bookkeeping."""

    started: set[str] = field(default_factory=set)
    requested: dict[str, dict] = field(default_factory=dict)
    prompt_open: bool = False  # an approval prompt is waiting for the user
    interrupted: bool = False  # Ctrl-C arrived while it was open
    announced: bool = False  # "turn cancelled" was already printed


def _render_event(event, summary, parts, console, progress, answer, calls: _Calls) -> None:
    """Render one turn event (approvals are handled by the caller)."""
    if isinstance(event, TextDelta):
        parts.append(event.text)
        answer.append(event.text)
    elif isinstance(event, ToolStarted):
        answer.stop()
        calls.started.add(event.call_id)
        progress.start(event.call_id, event.name, event.args)
    elif isinstance(event, NeedsApproval):
        answer.stop()
        calls.requested[event.request.call_id] = event.request.args
    elif isinstance(event, ToolFinished):
        summary.tools.add(event.name)
        if event.call_id not in calls.started:
            # refused before it ran (rejected, denied by a rule, unknown tool):
            # still give it its "· error" line
            answer.stop()
            progress.start(event.call_id, event.name, calls.requested.get(event.call_id, {}))
        progress.finish(event.call_id, event.ok, event.detail)
    elif isinstance(event, Notice):
        answer.stop()
        level = engine.notice_level(event.text) if event.level == "info" else event.level
        console.print(event.text, style=_NOTICE_STYLE.get(level, ""), markup=False)
    elif isinstance(event, TodosUpdated):
        answer.stop()
        lines = (f"{_TODO_MARKS.get(t.get('status'), '·')} {t.get('content')}" for t in event.todos)
        console.print("\n".join(lines), style="dim", markup=False)
    elif isinstance(event, Compacted):
        answer.stop()
        console.print(f"compacted {event.removed} messages into a summary", style="dim")
    elif isinstance(event, ReloadRequested):
        summary.reload = True
    elif isinstance(event, TurnDone):
        summary.usage, summary.cost = event.usage, event.cost
    elif isinstance(event, TurnError):
        answer.stop()
        summary.error = event.message
        console.print(f"turn failed: {event.message}", style=PALETTE["mauve"], markup=False)
    elif isinstance(event, Cancelled):
        answer.stop()
        summary.cancelled = True
        if not calls.announced:
            console.print("turn cancelled", style=PALETTE["mauve"])


async def run_once(
    bundle,
    prompt: str,
    *,
    console: Console,
    log,
    session: Session | None = None,
    cfg: LunaConfig,
    input_fn: Callable[[str], str] = input,
    output_format: str = "text",
    hooks_state: dict | None = None,
) -> str:
    """Run a single prompt; returns the answer text (``""`` in json mode, which prints JSON).

    ``hooks_state`` is the dict the bundle's engine hooks read the session
    state from (see :func:`run_repl`); it is pointed at this run's state.
    """
    workdir = str(bundle.agent.workspace.root)
    session = session or Session.create(log, workdir, title=make_title(prompt))
    state = SessionState(session.id, session.workdir)
    if hooks_state is not None:
        hooks_state["state"] = state
    names = {n for n, _ in subagent_summaries(session.workdir)}
    prepared = engine.prepare_turn(state, prompt, workdir=session.workdir, subagent_names=names)
    if not session.title:
        log.set_title(session.id, make_title(prepared.title_line))
    await asyncio.to_thread(engine.before_turn, state)
    with contextlib.ExitStack() as stack:
        quiet = console
        if output_format == "json":
            quiet = Console(file=stack.enter_context(open(os.devnull, "w")))  # noqa: SIM115
        result = await render_turn(
            bundle.agent,
            session,
            prepared.text,
            console=quiet,
            input_fn=input_fn,
            images=prepared.images,
        )
    if output_format == "json":
        print(
            json.dumps(
                {
                    "text": result.text,
                    "tools_used": sorted(result.tools),
                    "usage": {
                        "input": result.usage.input,
                        "output": result.usage.output,
                        "total": result.usage.total,
                    },
                    "cost_usd": result.cost,
                    "session_id": session.id,
                    "error": result.error or None,
                }
            )
        )
        return ""
    return result.text


#: Log records that close a turn (the last one says how the session was left).
_TURN_ENDS = ("turn_done", "cancelled", "turn_error")


def print_recap(session: Session, console: Console, keep: int = 6) -> None:
    """Print the tail of a resumed session's conversation (best effort)."""
    messages = [m for m in session.messages() if m.text.strip()]
    if not messages:
        return
    console.print(f"[{PALETTE['blue']}]— resuming, last {min(keep, len(messages))} messages —[/]")
    for message in messages[-keep:]:
        console.print(f"[dim]{message.role}:[/] ", end="")
        console.print(message.text[:200], markup=False, highlight=False)
    last = next((r.type for r in reversed(session.records()) if r.type in _TURN_ENDS), "")
    if last == "cancelled":
        console.print("[dim]прервано[/]")


async def _rebuild_bundle(ctx: CommandContext, console: Console) -> None:
    """Auto-reload after a turn: the new bundle replaces the old one only when it builds."""
    try:
        await swap_bundle(ctx)
    except Exception as exc:  # noqa: BLE001 - a bad config must not kill the session
        console.print(f"[{PALETTE['mauve']}]auto-reload failed: {exc}[/]")
        return
    console.print(f"[{PALETTE['blue']}]auto-reloaded — new capabilities are live[/]")


async def run_repl(
    bundle,
    *,
    console: Console,
    input_fn: Callable[[str], str] = input,
    rebuild: Callable | None = None,
    log,
    session: Session,
    config: LunaConfig,
    plan_state: list | None = None,
    hooks_state: dict | None = None,
) -> int:
    """Interactive line REPL (non-TTY fallback). Returns an exit code.

    ``rebuild`` is an async callable returning a new bundle. ``hooks_state``
    is the dict the engine hooks of ``bundle`` read the session state from
    (``engine.make_hooks(lambda: hooks_state.get("state"), ...)``); the REPL
    keeps it pointing at the current session's state.
    """
    plan_state = plan_state if plan_state is not None else [False]
    hooks_state = hooks_state if hooks_state is not None else {}
    store = SessionStateStore()
    state = store.load(session.id, session.workdir)
    hooks_state["state"] = state
    console.print(f"[{PALETTE['peri']}]Luna is ready. Type /help for commands.[/]\n")
    ctx = CommandContext(
        console=console,
        config=config,
        bundle=bundle,
        rebuild=rebuild,
        session=session,
        log=log,
        state=state,
        input_fn=input_fn,
        user_commands=usercmd.load(session.workdir),
        plan_state=plan_state,
    )
    if session.has_user_messages:
        print_recap(session, console)
    while True:
        try:
            label = "luna (plan) › " if plan_state[0] else "luna › "
            line = (await in_thread(input_fn, label)).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if not line:
            continue
        reload_after = False
        if line.startswith("/"):
            res = await dispatch(line, ctx)
            hooks_state["state"] = ctx.state
            if res.exit:
                return 0
            if res.prompt is None:
                continue
            line, reload_after = res.prompt, res.reload_after
        session, state = ctx.session, ctx.state
        names = {n for n, _ in subagent_summaries(session.workdir)}
        prepared = engine.prepare_turn(state, line, workdir=session.workdir, subagent_names=names)
        if not session.title:
            log.set_title(session.id, make_title(prepared.title_line))
        await asyncio.to_thread(engine.before_turn, state)
        try:
            result = await render_turn(
                ctx.bundle.agent,
                session,
                prepared.text,
                console=console,
                input_fn=input_fn,
                images=prepared.images,
            )
        except Exception as exc:  # noqa: BLE001 - an unexpected failure must not kill the session
            console.print(f"turn failed: {exc}", style=PALETTE["mauve"], markup=False)
            continue
        finally:
            store.save(state)
        if result.usage.total:
            cost = f" · ${result.cost:.4f}" if result.cost else ""
            console.print(
                f"[dim]{result.usage.input} in · {result.usage.output} out{cost}[/]",
                highlight=False,
            )
        if (result.reload or reload_after) and rebuild is not None:
            await _rebuild_bundle(ctx, console)
