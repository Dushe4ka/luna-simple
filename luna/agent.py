"""Assemble the Luna deep agent from a resolved config.

All ``deepagents`` / ``langgraph`` imports are confined to this module and
``luna.session``.
"""

from __future__ import annotations

from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import InMemorySaver

from luna.config import LunaConfig
from luna.prompts import LUNA_SYSTEM_PROMPT
from luna.providers import build_model

# Tools that mutate the workspace and therefore pause for approval.
INTERRUPT_TOOLS: dict = {
    "write_file": True,
    "edit_file": True,
    "delete": True,
    "execute": {"allowed_decisions": ["approve", "edit", "reject"]},
}


def build_agent(
    config: LunaConfig,
    *,
    model: BaseChatModel | None = None,
    checkpointer=None,
):
    """Build a compiled Luna deep agent.

    Args:
        config: resolved runtime settings.
        model: inject a model instance to bypass provider resolution
            (used by tests).
        checkpointer: LangGraph checkpointer; defaults to an in-memory one.

    """
    workdir = Path(config.workdir).resolve()
    backend = LocalShellBackend(root_dir=str(workdir), virtual_mode=False, inherit_env=True)
    memory = ["AGENTS.md"] if (workdir / "AGENTS.md").is_file() else None
    return create_deep_agent(
        model=model or build_model(config.provider, config.model, config.model_kwargs),
        system_prompt=LUNA_SYSTEM_PROMPT,
        backend=backend,
        memory=memory,
        interrupt_on=None if config.yolo else INTERRUPT_TOOLS,
        checkpointer=checkpointer or InMemorySaver(),
        name="luna",
    )
