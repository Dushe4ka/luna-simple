"""TEMPORARY: the old deepagents/LangChain agent for callers not yet on luna-core.

The REPL, the HTTP server and their tests still drive a LangGraph agent
(``agent.invoke`` / ``agent.stream`` with a checkpointer). Until they are
ported (Tasks 29-32) they build it here; :mod:`luna.core.agent` is the new
luna-core builder. Everything in this module is deleted with its last
caller — do not add new importers.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from deepagents import SubAgent, create_deep_agent
from deepagents.backends import LocalShellBackend
from deepagents.middleware import FilesystemMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import StructuredTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from luna.config.config import LunaConfig
from luna.config.prompts import LUNA_SYSTEM_PROMPT
from luna.config.providers import LunaConfigError, ProviderSpec, merge_providers
from luna.core.permissions import load_rules
from luna.core.toolguard import tool_guard
from luna.extensions import lspnav, mcp, skills, websearch
from luna.extensions.registry import known_mcp, known_skills, resolve_mcp
from luna.extensions.subagents import load_subagents
from luna.turn.memory import append_note, memory_files

# Tools that mutate the workspace and therefore pause for approval.
INTERRUPT_TOOLS: dict = {
    "write_file": True,
    "edit_file": True,
    "delete": True,
    "execute": {"allowed_decisions": ["approve", "edit", "reject"]},
}
EXTENSION_INTERRUPTS = {
    "manage_mcp": True,
    "manage_skills": True,
    "remember": True,
    "save_skill": True,
}

# LangChain ``init_chat_model`` prefix and pip extra per provider; anything
# missing here is OpenAI-compatible and reached through ``openai`` + base_url.
_LC_PREFIX: dict[str, tuple[str, str]] = {
    "anthropic": ("anthropic", "anthropic"),
    "deepseek": ("deepseek", "deepseek"),
    "openai": ("openai", "openai"),
    "google": ("google_genai", "google"),
    "ollama": ("ollama", "ollama"),
    "mistral": ("mistralai", "mistral"),
    "xai": ("xai", "xai"),
    "groq": ("groq", "groq"),
    "fireworks": ("fireworks", "fireworks"),
    "together": ("together", "together"),
    "openrouter": ("openrouter", "openrouter"),
    "perplexity": ("perplexity", "perplexity"),
}


def build_model_legacy(
    provider: str,
    model: str | None = None,
    model_kwargs: dict | None = None,
    *,
    registry: dict[str, ProviderSpec] | None = None,
) -> BaseChatModel:
    """Instantiate a LangChain chat model for ``provider`` (legacy path)."""
    from luna.config.providers import PROVIDERS

    reg = PROVIDERS if registry is None else registry
    spec = reg.get(provider)
    if spec is None:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(sorted(reg))}."
        )
    if spec.env_var and not os.environ.get(spec.env_var):
        from luna.config.credentials import apply_stored_key

        apply_stored_key(provider, spec.env_var)
    if spec.env_var and not os.environ.get(spec.env_var):
        raise LunaConfigError(
            f"No API key for {spec.key}. Run 'luna setup' to configure one, "
            f"export {spec.env_var}, or pick another provider with --provider."
        )
    from langchain.chat_models import init_chat_model

    prefix, extra = _LC_PREFIX.get(provider, ("openai", "openai"))
    kwargs = dict(model_kwargs or {})
    if provider not in _LC_PREFIX and spec.base_url:
        kwargs.setdefault("base_url", spec.base_url)
        if spec.env_var:
            kwargs.setdefault("api_key", os.environ[spec.env_var])
    try:
        return init_chat_model(f"{prefix}:{model or spec.default_model}", **kwargs)
    except ImportError as exc:
        raise LunaConfigError(
            f'The {spec.key} integration is not installed. Run:  pip install "luna-simple[{extra}]"'
        ) from exc


def _model_kwargs(config: LunaConfig) -> dict:
    # max_retries=0: a provider SDK retry restarts a streamed generation and
    # duplicates already-shown text; Luna's own turn-level retry handles it.
    kwargs: dict = {"max_retries": 0}
    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    return kwargs


# -- LangChain versions of the extension tools ------------------------------


@tool("manage_mcp")
def _manage_mcp(
    action: Literal["add", "remove", "list"],
    name: str | None = None,
    command: str | None = None,
    command_args: list[str] | None = None,
) -> str:
    """Add, remove, or list MCP servers for Luna.

    For ``add``: resolve ``name`` from the registry, or pass an explicit
    ``command`` plus ``command_args``. Newly added servers activate after the
    user runs ``/reload``.
    """
    if action == "list":
        configured = ", ".join(mcp.load_mcp_config()) or "(none configured)"
        return f"configured: {configured}\nknown: {', '.join(known_mcp())}"
    if not name:
        return "name is required for add/remove"
    if action == "remove":
        return f"removed {name!r}" if mcp.remove_server(name) else f"{name!r} was not configured"
    try:
        spec = {"command": command, "args": command_args or []} if command else resolve_mcp(name)
    except LunaConfigError as exc:
        return str(exc)
    mcp.add_server(name, spec)
    return f"added MCP server {name!r}. Run /reload to activate."


@tool("manage_skills")
def _manage_skills(
    action: Literal["add", "remove", "list"],
    name: str | None = None,
    source: str | None = None,
) -> str:
    """Add, remove, or list Luna skills.

    For ``add``: ``source`` is a registry name, ``owner/repo``, or
    ``owner/repo/subdir``. Newly added skills activate after the user runs
    ``/reload``.
    """
    if action == "list":
        installed = ", ".join(f"{s} ({scope})" for scope, s, _ in skills.list_skills())
        return f"installed: {installed or '(none)'}\nknown: {', '.join(known_skills())}"
    if action == "remove":
        return f"removed {name!r}" if name and skills.remove(name) else f"{name!r} is not installed"
    target = source or name
    if not target:
        return "source (or name) is required for add"
    try:
        return skills.install(target, name=name if source else None)
    except LunaConfigError as exc:
        return str(exc)


@tool("save_skill")
def _save_skill(name: str, description: str, body: str, project: bool = True) -> str:
    """Save a successful, reusable pattern as a new local Luna skill.

    Use this after completing a nontrivial multi-step task you expect to
    repeat — body should be the actual reusable instructions/steps, not a
    narrative of what just happened. Defaults to project scope
    (.luna/skills/); pass project=False only for a pattern that is not
    specific to this repository. Activates after the user runs /reload.
    """
    existing = [n for _, n, _ in skills.list_skills() if n == name]
    try:
        path = skills.save(name, description, body, project=project)
    except LunaConfigError as exc:
        return str(exc)
    verb = "replaced" if existing else "saved"
    return f"{verb} skill at {path.as_posix()}. Run /reload to activate."


@tool("remember")
def _remember(
    kind: Literal["project", "conventions", "decisions", "failures"],
    topic: str,
    note: str,
) -> str:
    """Record a durable note in .luna/memory/<kind>.md.

    Use 'failures' for dead ends ("tried X, it conflicts with Y, don't retry"),
    'decisions' for load-bearing choices, 'conventions' for how this repo works,
    'project' for what it is. Run /reload afterwards to load it into context.
    """
    path = append_note(".", kind, topic, note)
    return f"noted in {path.as_posix()}. Run /reload to load it into context."


@tool("web_search")
def _web_search(query: str, max_results: int = 5) -> str:
    """Search the web and return the top results (title, URL, snippet).

    Uses Tavily (TAVILY_API_KEY) or Brave (BRAVE_API_KEY) when configured and
    falls back to a free, keyless search otherwise.
    """
    return websearch._search(query, max_results)


LEGACY_EXTENSION_TOOLS = [_manage_mcp, _manage_skills, _remember, _save_skill, _web_search]


def _lsp_tools(workdir: str, language: str) -> list:
    if not lspnav.available():
        return []
    return [
        StructuredTool.from_function(fn, name=fn.__name__.lstrip("_"))
        for fn in lspnav._functions(workdir, language)
    ]


# -- MCP via langchain-mcp-adapters -----------------------------------------


def to_connections(servers: Mapping[str, dict]) -> dict[str, dict]:
    """Translate Claude-style server specs into langchain connection dicts."""
    out: dict[str, dict] = {}
    for name, spec in servers.items():
        kind = spec.get("type") or spec.get("transport")
        if "command" in spec and kind in (None, "stdio"):
            conn = {
                "transport": "stdio",
                "command": spec["command"],
                "args": list(spec.get("args", [])),
            }
            if spec.get("env"):
                conn["env"] = dict(spec["env"])
        elif kind in ("http", "streamable_http") and spec.get("url"):
            conn = {"transport": "streamable_http", "url": spec["url"]}
            if spec.get("headers"):
                conn["headers"] = dict(spec["headers"])
        elif kind == "sse" and spec.get("url"):
            conn = {"transport": "sse", "url": spec["url"]}
            if spec.get("headers"):
                conn["headers"] = dict(spec["headers"])
        else:
            raise LunaConfigError(
                f"MCP server {name!r}: need a 'command' (stdio) or a 'url' with type 'http'/'sse'."
            )
        out[name] = conn
    return out


def load_mcp_tools(connections: Mapping[str, dict], *, on_warn: Callable[[str], None]) -> list:
    """Discover tools from every configured MCP server (``[]`` and a warning on failure)."""
    if not connections:
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        on_warn('MCP support not installed. Run: pip install "luna-simple[mcp]"')
        return []
    try:
        client = MultiServerMCPClient(dict(connections), tool_name_prefix=True)
        return asyncio.run(client.get_tools())
    except Exception as exc:  # noqa: BLE001 - one bad server must not break Luna
        on_warn(f"MCP: could not start servers ({exc}). Try 'luna mcp test <name>'.")
        return []


# -- subagents as deepagents dicts ------------------------------------------


def _subagents(
    workdir: str, fast_model: str | None, guard: object, backend: object, on_warn
) -> list[SubAgent]:
    agents: list[SubAgent] = []
    for spec in load_subagents(workdir, on_warn=on_warn).values():
        if spec.name == "general-purpose":
            continue  # deepagents brings its own general-purpose subagent
        model = spec.model or fast_model
        fs_tools = [t for t in spec.tools if t != "write_todos"]
        entry: dict = {
            "name": spec.name,
            "description": spec.description,
            "system_prompt": spec.prompt,
            "middleware": [guard, FilesystemMiddleware(backend=backend, tools=fs_tools)],
        }
        if model:
            entry["model"] = model
        agents.append(SubAgent(**entry))
    return agents


def build_agent_legacy(
    config: LunaConfig,
    *,
    model: BaseChatModel | None = None,
    checkpointer=None,
    on_warn: Callable[[str], None] = print,
    session_id: str = "",
    plan_flag: Callable[[], bool] | None = None,
):
    """Build the compiled deepagents Luna agent (old signature, sync)."""
    workdir = Path(config.workdir).resolve()
    if config.ignored_extra:
        on_warn(
            "[agent] extra is no longer used (it held LangChain model kwargs); "
            "use temperature, max_tokens and reasoning"
        )
    language = config.language or lspnav.detect_language(str(workdir))
    lsp_tools: list = []
    if language:
        if lspnav.available():
            lsp_tools = _lsp_tools(str(workdir), language)
        else:
            on_warn(
                f"a '{language}' project was detected but the 'lsp' extra "
                f"(multilspy) is not installed — goto_definition/find_references/"
                f"hover/symbol_range are unavailable"
            )
    # virtual_mode maps the agent's "/" to workdir: real files, confined to the repo.
    backend = LocalShellBackend(root_dir=str(workdir), virtual_mode=True, inherit_env=True)
    mem = (["AGENTS.md"] if (workdir / "AGENTS.md").is_file() else []) + memory_files(str(workdir))
    rules = load_rules(str(workdir))
    guard = tool_guard(rules, str(workdir), session_id=session_id, plan=plan_flag)
    skill_dirs = [str(d) for d in skills.existing_skill_dirs(config.workdir)]
    servers = mcp.load_mcp_config(config.workdir)
    mcp_tools = load_mcp_tools(to_connections(servers), on_warn=on_warn) if servers else []
    subs = _subagents(config.workdir, config.fast_model, guard, backend, on_warn)
    interrupt_on = None if config.yolo else {**INTERRUPT_TOOLS, **EXTENSION_INTERRUPTS}
    return create_deep_agent(
        model=model
        or build_model_legacy(
            config.provider,
            config.model,
            _model_kwargs(config),
            registry=merge_providers(config.custom_providers),
        ),
        system_prompt=LUNA_SYSTEM_PROMPT,
        backend=backend,
        memory=mem or None,
        tools=[*LEGACY_EXTENSION_TOOLS, *mcp_tools, *lsp_tools],
        skills=skill_dirs or None,
        subagents=subs or None,
        interrupt_on=interrupt_on,
        middleware=[guard],
        checkpointer=checkpointer or InMemorySaver(),
        name="luna",
    )
