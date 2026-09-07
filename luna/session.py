"""Streaming REPL / one-shot session loop with approval handling.

Together with :mod:`luna.agent` this is the only module that touches
``deepagents`` / ``langgraph`` directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.types import Command
from rich.console import Console

from luna.commands import HELP as SLASH_COMMANDS
from luna.commands import CommandContext, dispatch
from luna.config import LunaConfig
from luna.persistence import SessionIndex, make_title
from luna.ui.approve import prompt_decision
from luna.ui.theme import PALETTE
from luna.ui.turn import close_turn, open_turn, tool_line

__all__ = ["SLASH_COMMANDS", "collect_decisions", "run_once", "run_repl"]

_RELOAD_MARKER = "Run /reload"


def _new_thread_id() -> str:
    return uuid.uuid4().hex


def collect_decisions(
    console: Console,
    interrupt_value: dict,
    *,
    input_fn: Callable[[str], str] = input,
) -> dict:
    """Turn an interrupt payload into a ``Command(resume=...)`` argument."""
    requests = interrupt_value.get("action_requests")
    if requests is None and "action_request" in interrupt_value:
        requests = [interrupt_value["action_request"]]
    decisions = [prompt_decision(console, request, input_fn=input_fn) for request in requests or []]
    return {"decisions": decisions}


def _iter_interrupts(chunk: object):
    if isinstance(chunk, dict) and "__interrupt__" in chunk:
        yield from chunk["__interrupt__"]


def _report_tools(chunk: dict, console: Console, seen: set[str]) -> bool:
    """Print tool lines; return True if a tool asked for /reload."""
    reload_requested = False
    for update in chunk.values():
        if not isinstance(update, dict):
            continue
        for msg in update.get("messages", []) or []:
            if isinstance(msg, ToolMessage) and msg.tool_call_id not in seen:
                seen.add(msg.tool_call_id)
                body = str(msg.content) if msg.content else ""
                tool_line(console, msg.name or "tool", body.splitlines()[0][:120])
                if msg.name in ("manage_mcp", "manage_skills") and _RELOAD_MARKER in body:
                    reload_requested = True
    return reload_requested


def _stream_turn(
    agent,
    payload,
    config: dict,
    console: Console,
    input_fn: Callable[[str], str],
) -> tuple[str, bool]:
    """Run one user turn to completion. Returns ``(final_text, reload_requested)``."""
    parts: list[str] = []
    seen_tools: set[str] = set()
    reload_requested = False

    open_turn(console)
    while True:
        interrupts: list = []
        for mode, chunk in agent.stream(
            payload, config=config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                msg, meta = chunk
                if meta.get("langgraph_node") == "model" and isinstance(
                    msg, (AIMessage, AIMessageChunk)
                ):
                    text = msg.content if isinstance(msg.content, str) else ""
                    if text:
                        parts.append(text)
                        console.print(text, end="", soft_wrap=True)
            elif mode == "updates":
                interrupts.extend(_iter_interrupts(chunk))
                reload_requested |= _report_tools(chunk, console, seen_tools)

        if not interrupts:
            state = agent.get_state(config)
            interrupts = list(getattr(state, "interrupts", ()) or [])
        if not interrupts:
            break

        console.print()
        resume = collect_decisions(console, interrupts[0].value, input_fn=input_fn)
        payload = Command(resume=resume)

    close_turn(console)
    return "".join(parts).strip(), reload_requested


def run_once(
    agent,
    prompt: str,
    *,
    thread_id: str | None = None,
    console: Console,
    input_fn: Callable[[str], str] = input,
    index: SessionIndex | None = None,
    workdir: str = ".",
) -> str:
    """Run a single prompt and return the final assistant text."""
    thread_id = thread_id or _new_thread_id()
    config = {"configurable": {"thread_id": thread_id}}
    payload = {"messages": [{"role": "user", "content": prompt}]}
    text, _ = _stream_turn(agent, payload, config, console, input_fn)
    if index is not None:
        index.record(thread_id, workdir, make_title(prompt))
        index.touch(thread_id)
    return text


def _print_recap(agent, config, console, keep=6):
    """Print the tail of a resumed thread's history (best effort)."""
    try:
        msgs = agent.get_state(config).values.get("messages", [])
    except Exception:  # noqa: BLE001 - recap is cosmetic
        return
    if not msgs:
        return
    console.print(f"[{PALETTE['blue']}]— resuming, last {min(keep, len(msgs))} messages —[/]")
    for m in msgs[-keep:]:
        role = getattr(m, "type", "?")
        text = (getattr(m, "content", "") or "")[:200]
        if text:
            console.print(f"[dim]{role}:[/] {text}")


def run_repl(
    agent,
    *,
    console: Console,
    input_fn: Callable[[str], str] = input,
    rebuild: Callable[[], object] | None = None,
    index: SessionIndex | None = None,
    thread_id: str | None = None,
    workdir: str = ".",
    config: LunaConfig | None = None,
) -> int:
    """Interactive loop. Returns a process exit code."""
    thread_id = thread_id or _new_thread_id()
    console.print(f"[{PALETTE['peri']}]Luna is ready. Type /help for commands.[/]\n")

    ctx = CommandContext(
        console=console,
        config=config or LunaConfig(),
        agent=agent,
        rebuild=rebuild,
        thread_id=thread_id,
        workdir=workdir,
        index=index,
    )

    if index is not None:
        _print_recap(agent, {"configurable": {"thread_id": thread_id}}, console)

    while True:
        try:
            line = input_fn("luna › ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0

        if not line:
            continue

        if line.startswith("/"):
            ctx.agent = agent
            ctx.thread_id = thread_id
            res = dispatch(line, ctx)
            if res.exit:
                return 0
            if res.handled:
                if res.agent is not None:
                    agent = res.agent
                if res.thread_id is not None:
                    thread_id = res.thread_id
                continue

        turn_config = {"configurable": {"thread_id": thread_id}}
        payload = {"messages": [{"role": "user", "content": line}]}
        try:
            _, reload_requested = _stream_turn(agent, payload, turn_config, console, input_fn)
        except KeyboardInterrupt:
            console.print(f"\n[{PALETTE['mauve']}]turn cancelled[/]")
            continue
        if index is not None:
            index.record(thread_id, workdir, make_title(line))
            index.touch(thread_id)
        if reload_requested and rebuild is not None:
            agent = rebuild()
            console.print(f"[{PALETTE['blue']}]auto-reloaded — new capabilities are live[/]")
