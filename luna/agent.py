"""Assemble the Luna deep agent from a resolved config.

All ``deepagents`` / ``langgraph`` / ``langchain_mcp_adapters`` imports are
confined to this module and ``luna.session``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import InMemorySaver

from luna import mcp as mcp_mod
from luna import skills as skills_mod
from luna import subagents as subagents_mod
from luna.config import LunaConfig
from luna.extension_tools import EXTENSION_INTERRUPTS, EXTENSION_TOOLS
from luna.memory import memory_files
from luna.permissions import load_rules
from luna.prompts import LUNA_SYSTEM_PROMPT
from luna.providers import build_model
from luna.toolguard import tool_guard

# Tools that mutate the workspace and therefore pause for approval.
INTERRUPT_TOOLS: dict = {
    "write_file": True,
    "edit_file": True,
    "delete": True,
    "execute": {"allowed_decisions": ["approve", "edit", "reject"]},
}


def _extension_bits(
    config: LunaConfig,
    on_warn: Callable[[str], None],
    guard: object | None = None,
    backend: object | None = None,
):
    skill_dirs = [str(d) for d in skills_mod.existing_skill_dirs(config.workdir)]
    servers = mcp_mod.load_mcp_config(config.workdir)
    mcp_tools = (
        mcp_mod.load_mcp_tools(mcp_mod.to_connections(servers), on_warn=on_warn) if servers else []
    )
    subs = subagents_mod.load_subagents(
        config.workdir,
        fast_model=config.fast_model,
        guard=guard,
        on_warn=on_warn,
        backend=backend,
    )
    return skill_dirs, list(servers), mcp_tools, subs


def build_agent(
    config: LunaConfig,
    *,
    model: BaseChatModel | None = None,
    checkpointer=None,
    on_warn: Callable[[str], None] = print,
    session_id: str = "",
):
    """Build a compiled Luna deep agent.

    Args:
        config: resolved runtime settings.
        model: inject a model instance to bypass provider resolution (tests).
        checkpointer: LangGraph checkpointer; defaults to an in-memory one.
        on_warn: sink for non-fatal warnings (e.g. an MCP server that failed).
        session_id: per-process id for the undo journal (``/diff`` and ``/undo``).

    """
    workdir = Path(config.workdir).resolve()
    # virtual_mode maps the agent's "/" to workdir: real files, confined to the repo.
    backend = LocalShellBackend(root_dir=str(workdir), virtual_mode=True, inherit_env=True)
    mem = (["AGENTS.md"] if (workdir / "AGENTS.md").is_file() else []) + memory_files(str(workdir))
    memory = mem or None
    rules = load_rules(str(workdir))
    # One guard instance for the main agent and every subagent: shared deny rules
    # and a single per-session undo journal for all changes made this session.
    guard = tool_guard(rules, str(workdir), session_id=session_id)
    skill_dirs, _servers, mcp_tools, subs = _extension_bits(
        config, on_warn, guard=guard, backend=backend
    )
    interrupt_on = None if config.yolo else {**INTERRUPT_TOOLS, **EXTENSION_INTERRUPTS}
    return create_deep_agent(
        model=model or build_model(config.provider, config.model, config.model_kwargs),
        system_prompt=LUNA_SYSTEM_PROMPT,
        backend=backend,
        memory=memory,
        tools=[*EXTENSION_TOOLS, *mcp_tools],
        skills=skill_dirs or None,
        subagents=subs or None,
        interrupt_on=interrupt_on,
        middleware=[guard],
        checkpointer=checkpointer or InMemorySaver(),
        name="luna",
    )


def describe_capabilities(config: LunaConfig) -> dict:
    """Summarise what ``build_agent`` would wire in right now (for ``/reload``)."""
    _skill_dirs, servers, mcp_tools, subs = _extension_bits(config, lambda *_: None)
    return {
        "tools": len(EXTENSION_TOOLS) + len(mcp_tools),
        "mcp": servers,
        "skills": [name for _, name, _ in skills_mod.list_skills(config.workdir)],
        "subagents": [s["name"] for s in subs],
    }
