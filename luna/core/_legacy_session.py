"""LangGraph-era ``/compact`` for the local server's agents.

TEMPORARY: the REPL runs on luna-core (:mod:`luna.core.session`); the
server's ``/compact`` still goes through this until the server and the
commands are ported, after which the module goes away.
"""

from __future__ import annotations

import uuid

from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command
from rich.console import Console

from luna.ui.theme import PALETTE

__all__ = ["compact_history", "compact_thread"]

_COMPACT_ASK = (
    "Summarise this whole session as a dense handoff note: the goal, decisions "
    "made, files touched, current state, and open questions. Text only — do not "
    "call any tools. No preamble."
)


def compact_history(agent, thread_id: str) -> tuple[bool, str]:
    """Replace this thread's history with a model-written summary: (ok, message)."""
    config = {"configurable": {"thread_id": thread_id}}
    pre = agent.get_state(config).values.get("messages", [])
    result = agent.invoke({"messages": [{"role": "user", "content": _COMPACT_ASK}]}, config)
    if isinstance(result, dict) and result.get("__interrupt__"):
        result = agent.invoke(
            Command(resume={"decisions": [{"type": "reject", "message": "summary only"}]}),
            config,
        )
    summary = ""
    messages = result.get("messages", []) if isinstance(result, dict) else []
    for msg in reversed(messages):
        text = getattr(msg, "content", "")
        if getattr(msg, "type", "") == "ai" and isinstance(text, str) and text.strip():
            summary = text.strip()
            break
    if not summary:
        added = agent.get_state(config).values["messages"][len(pre) :]
        agent.update_state(config, {"messages": [RemoveMessage(id=m.id) for m in added]})
        return False, "/compact: no summary produced"
    agent.update_state(
        config,
        {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                HumanMessage(id=uuid.uuid4().hex, content="[compacted] Handoff note:\n" + summary),
            ]
        },
    )
    return True, "compacted — history replaced with a summary"


def compact_thread(agent, thread_id: str, console: Console) -> None:
    """Replace this thread's message history with a model-written summary, in place."""
    ok, message = compact_history(agent, thread_id)
    console.print(f"[{PALETTE['blue'] if ok else PALETTE['mauve']}]{message}[/]")
