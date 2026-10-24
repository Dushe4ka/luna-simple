"""Build the Luna agent on luna-core from a resolved LunaConfig."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from luna_core import skills as core_skills
from luna_core.background import BackgroundTasks
from luna_core.compaction import Compactor
from luna_core.context import build_system_prompt
from luna_core.hooks import Hooks, load_command_hooks
from luna_core.loop import Agent
from luna_core.mcp import MCPManager
from luna_core.memory import remember
from luna_core.policy import DEFAULT_APPROVAL, Policy, Rules, RulesFileError, append_rule
from luna_core.providers.base import Provider
from luna_core.roles import ModelRoles
from luna_core.sandbox import DockerSandbox, LocalSandbox
from luna_core.subagents import SubagentRunner, task_tool
from luna_core.tools import builtin_tools
from luna_core.tools.base import Tool
from luna_core.tools.tasks import task_cancel_tool, task_status_tool
from luna_core.workspace import Workspace

from luna.config.config import LunaConfig, config_dir
from luna.config.prompts import LUNA_SYSTEM_PROMPT
from luna.config.providers import (
    LunaConfigError,
    ProviderSpec,
    build_provider,
    merge_providers,
)
from luna.extensions import lspnav
from luna.extensions import mcp as mcp_mod
from luna.extensions.extension_tools import EXTENSION_APPROVALS, EXTENSION_TOOLS
from luna.extensions.subagents import load_subagents

__all__ = [
    "EXTENSION_APPROVALS",
    "AgentBundle",
    "build_agent",
    "describe_capabilities",
    "skill_dirs",
]


@dataclass
class AgentBundle:
    """An agent plus the resources that must be closed with it."""

    agent: Agent
    mcp: MCPManager | None = None

    async def aclose(self) -> None:
        """Stop MCP servers and the sandbox."""
        if self.mcp is not None:
            await self.mcp.close()
        await self.agent.sandbox.close()


def skill_dirs(workdir: str | Path) -> list[tuple[str, Path]]:
    """User skills first, then the project's (a project skill overrides a user one)."""
    return [("user", config_dir() / "skills"), ("project", Path(workdir) / ".luna" / "skills")]


def _roles(config: LunaConfig, registry: dict[str, ProviderSpec]) -> ModelRoles:
    """Model roles from config: ``"model"`` (same provider) or ``"provider:model"``."""
    targets = dict(config.roles)
    if config.fast_model:  # ``fast_model`` is the alias of ``roles["fast"]``
        targets["fast"] = config.fast_model
    values: dict = {}
    for role, target in targets.items():
        name, sep, rest = target.partition(":")
        if sep and name in registry:
            values[role] = build_provider(name, rest or None, registry=registry)
        else:
            values[role] = target
    return ModelRoles(**values)


def _merge_tools(tools: list[Tool], extra: list[Tool], on_warn: Callable[[str], None]) -> None:
    """Append ``extra`` to ``tools``, skipping (with a warning) any name already taken."""
    names = {t.name for t in tools}
    for t in extra:
        if t.name in names:
            on_warn(f"MCP tool {t.name!r} skipped: a tool with that name already exists")
            continue
        names.add(t.name)
        tools.append(t)


async def build_agent(
    config: LunaConfig,
    *,
    provider: Provider | None = None,
    on_warn: Callable[[str], None] = print,
    plan_flag: Callable[[], bool] | None = None,
    hooks: Hooks | None = None,
    background: BackgroundTasks | None = None,
) -> AgentBundle:
    """Build a Luna agent.

    ``provider`` injects an adapter (tests pass a ``FakeProvider``); the
    model then defaults to ``config.model or "fake-model"``. Project files
    (``.luna/mcp.json``, ``subagents.toml``, ``[[hooks]]``) can run code, so
    callers must only build agents for trusted workdirs.
    """
    workdir = Path(config.workdir).resolve()
    workspace = Workspace(workdir)
    registry = merge_providers(config.custom_providers)
    if provider is None:
        provider, model = build_provider(config.provider, config.model, registry=registry)
    else:
        model = config.model or "fake-model"
    if config.ignored_extra:
        on_warn(
            "[agent] extra is no longer used (it held LangChain model kwargs); "
            "use temperature, max_tokens and reasoning"
        )
    project_rules = workdir / ".luna" / "permissions.toml"
    try:
        rules = Rules.load([config_dir() / "config.toml", project_rules])
    except RulesFileError as exc:  # fail closed: never run without the deny rules
        raise LunaConfigError(str(exc)) from exc
    policy = Policy(
        rules,
        approval_tools=DEFAULT_APPROVAL | EXTENSION_APPROVALS,
        yolo=config.yolo,
        plan_mode=plan_flag or False,
        workspace=workspace,
        persist_rule=lambda rule: append_rule(project_rules, rule),
    )
    tools: list[Tool] = [
        *builtin_tools(),
        remember,
        core_skills.skill_tool(lambda: skill_dirs(workdir)),
        core_skills.save_skill_tool(lambda project: skill_dirs(workdir)[1 if project else 0][1]),
        *EXTENSION_TOOLS,
    ]
    language = config.language or lspnav.detect_language(str(workdir))
    if language and lspnav.available():
        tools += lspnav.make_tools(str(workdir), language)
    elif language:
        on_warn(
            f"a '{language}' project was detected but the 'lsp' extra (multilspy) is not "
            "installed — goto_definition/find_references/hover/symbol_range are unavailable"
        )
    servers = mcp_mod.load_mcp_config(str(workdir))
    manager = MCPManager(servers, on_warn=on_warn) if servers else None
    try:
        runner = SubagentRunner(
            load_subagents(str(workdir), on_warn=on_warn), max_parallel=config.max_parallel
        )
        tools += [task_tool(runner), task_status_tool(), task_cancel_tool()]
        roles = _roles(config, registry)
        if manager is not None:
            _merge_tools(tools, await manager.start(), on_warn)
        all_hooks = Hooks()
        if hooks is not None:
            all_hooks.extend(hooks)
        all_hooks.extend(
            load_command_hooks(config.hooks, workdir, env_passthrough=config.execute_pass_env)
        )
        if config.sandbox == "docker":
            sandbox = DockerSandbox(
                workdir,
                image=config.sandbox_image,
                network=config.sandbox_network,
                env_passthrough=config.execute_pass_env,
            )
        else:
            sandbox = LocalSandbox(env_passthrough=config.execute_pass_env)
        summarize_provider, summarize_model = roles.get("summarize", provider, model)

        def system(session) -> str:
            return build_system_prompt(
                LUNA_SYSTEM_PROMPT, root=workdir, skills=core_skills.discover(skill_dirs(workdir))
            )

        agent = Agent(
            provider,
            model,
            tools=tools,
            system=system,
            policy=policy,
            workspace=workspace,
            sandbox=sandbox,
            hooks=all_hooks,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            reasoning=config.reasoning,
            max_cost=config.max_cost,
            compactor=Compactor(provider=summarize_provider, model=summarize_model),
            services={"background": background or BackgroundTasks(config.max_parallel)},
            model_overrides=config.pricing or None,
            roles=roles,
        )
        return AgentBundle(agent, manager)
    except BaseException:
        # any failure after the servers started must not leak their processes
        if manager is not None:
            await manager.close()
        raise


async def describe_capabilities(config: LunaConfig) -> dict:
    """Summarise what ``build_agent`` would wire in right now (for ``/reload``)."""
    from luna.extensions import skills as skills_mod

    workdir = str(Path(config.workdir).resolve())
    return {
        "mcp": list(mcp_mod.load_mcp_config(workdir)),
        "skills": [name for _, name, _ in skills_mod.list_skills(workdir)],
        "subagents": list(load_subagents(workdir)),
    }
