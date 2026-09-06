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

from luna.subagents import subagent_summaries
from luna.ui.approve import prompt_decision
from luna.ui.theme import PALETTE
from luna.ui.turn import close_turn, open_turn, tool_line

SLASH_COMMANDS: dict[str, str] = {
    "/help": "show this help",
    "/tools": "list the agent's tools",
    "/agents": "list available subagents",
    "/model": "show the active model",
    "/provider": "show the active provider",
    "/reload": "rebuild the agent with the current config (skills, MCP, subagents)",
    "/new": "start a fresh conversation thread",
    "/clear": "clear the screen",
    "/exit": "leave Luna (also /quit, Ctrl-D)",
}

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
) -> str:
    """Run a single prompt and return the final assistant text."""
    config = {"configurable": {"thread_id": thread_id or _new_thread_id()}}
    payload = {"messages": [{"role": "user", "content": prompt}]}
    text, _ = _stream_turn(agent, payload, config, console, input_fn)
    return text


def _print_help(console: Console) -> None:
    for name, help_text in SLASH_COMMANDS.items():
        console.print(f"  [bold {PALETTE['peri']}]{name}[/]  {help_text}")


_TOOL_NAMES = (
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "delete",
    "glob",
    "grep",
    "execute",
    "write_todos",
    "task",
    "manage_mcp",
    "manage_skills",
)


def _list_tools(console: Console) -> None:
    console.print("  " + ", ".join(_TOOL_NAMES))
    console.print(f"  [dim {PALETTE['blue']}](+ any MCP tools as mcp__<server>__<tool>)[/]")


def _list_agents(console: Console) -> None:
    for name, desc in subagent_summaries():
        console.print(f"  [bold {PALETTE['accent']}]{name}[/] — {desc}")


def run_repl(
    agent,
    *,
    console: Console,
    input_fn: Callable[[str], str] = input,
    rebuild: Callable[[], object] | None = None,
) -> int:
    """Interactive loop. Returns a process exit code."""
    thread_id = _new_thread_id()
    console.print(f"[{PALETTE['peri']}]Luna is ready. Type /help for commands.[/]\n")

    while True:
        try:
            line = input_fn("luna › ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0

        if not line:
            continue
        if line in ("/exit", "/quit"):
            return 0
        if line == "/help":
            _print_help(console)
            continue
        if line == "/tools":
            _list_tools(console)
            continue
        if line == "/agents":
            _list_agents(console)
            continue
        if line == "/clear":
            console.clear()
            continue
        if line == "/new":
            thread_id = _new_thread_id()
            console.print(f"[{PALETTE['blue']}]started a new thread[/]")
            continue
        if line == "/reload":
            if rebuild is None:
                console.print(f"[{PALETTE['mauve']}]/reload is not available here[/]")
            else:
                agent = rebuild()
                console.print(f"[{PALETTE['blue']}]reloaded — capabilities refreshed[/]")
            continue
        if line in ("/model", "/provider"):
            console.print(
                f"[{PALETTE['blue']}]{line[1:]}: set at startup — "
                f"restart with --{line[1:]} to change[/]"
            )
            continue
        if line.startswith("/"):
            console.print(f"[{PALETTE['mauve']}]unknown command {line!r}; try /help[/]")
            continue

        config = {"configurable": {"thread_id": thread_id}}
        payload = {"messages": [{"role": "user", "content": line}]}
        try:
            _, reload_requested = _stream_turn(agent, payload, config, console, input_fn)
        except KeyboardInterrupt:
            console.print(f"\n[{PALETTE['mauve']}]turn cancelled[/]")
            continue
        if reload_requested and rebuild is not None:
            agent = rebuild()
            console.print(f"[{PALETTE['blue']}]auto-reloaded — new capabilities are live[/]")
