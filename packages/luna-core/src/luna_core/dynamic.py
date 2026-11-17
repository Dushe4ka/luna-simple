"""Dynamic subagents: build a :class:`SubagentSpec` from the fields of a ``task`` call."""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping

from luna_core.errors import LunaCoreError
from luna_core.roles import ROLES
from luna_core.subagents import CHILD_FORBIDDEN, WRITE_TOOLS, DynamicSettings, SubagentSpec

DYNAMIC_FIELDS = ("prompt", "tools", "skills", "model", "context")


class SubagentRequestError(LunaCoreError):
    """The parent asked for a subagent it may not have; the message is for the model."""


def is_writing(name: str, parent_tools: Mapping[str, object]) -> bool:
    """Return True for tools that change files, run commands or are marked mutating."""
    return name in WRITE_TOOLS or bool(getattr(parent_tools.get(name), "mutating", False))


def _present(value: object) -> bool:
    return value is not None and value != "" and value != []


def _string_list(args: Mapping[str, object], key: str, what: str) -> tuple[str, ...]:
    value = args[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SubagentRequestError(f"{key} must be a list of {what}, got {value!r}")
    return tuple(dict.fromkeys(value))


def resolve_spec(
    base: SubagentSpec,
    args: Mapping[str, object],
    *,
    parent_tools: Mapping[str, object],
    skills: Collection[str],
    settings: DynamicSettings,
) -> tuple[SubagentSpec, bool]:
    """Return the spec the call asks for and whether it is a dynamic assembly.

    Raises :class:`SubagentRequestError` with a message the parent model can act on.
    """
    dynamic = any(key in args and _present(args[key]) for key in DYNAMIC_FIELDS)
    # An explicit empty list is a request too: it must fail, not fall back silently.
    if args.get("tools") == []:
        raise SubagentRequestError("tools must name at least one tool")
    changes: dict[str, object] = {}
    if "max_steps" in args and args["max_steps"] is not None:
        steps = args["max_steps"]
        if (
            isinstance(steps, bool)
            or not isinstance(steps, int)
            or not 1 <= steps <= settings.max_steps
        ):
            raise SubagentRequestError(
                f"max_steps must be an integer between 1 and {settings.max_steps}, got {steps!r}"
            )
        changes["max_steps"] = steps
    if not dynamic:
        return (dataclasses.replace(base, **changes) if changes else base), False
    if not settings.enabled:
        raise SubagentRequestError(
            "dynamic subagents are disabled ([subagents] dynamic = false); "
            "call task with subagent_type only"
        )

    if "prompt" in args and args["prompt"] is not None:
        prompt = args["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise SubagentRequestError("prompt must be a non-empty string")
        changes["prompt"] = prompt.strip()

    if "tools" in args and args["tools"] is not None:
        if not isinstance(args["tools"], list):
            raise SubagentRequestError(f"tools must be a list of tool names, got {args['tools']!r}")
        names = _string_list(args, "tools", "tool names")
        forbidden = sorted(set(names) & CHILD_FORBIDDEN)
        if forbidden:
            raise SubagentRequestError(
                f"a subagent cannot get {forbidden}: subagents do not delegate or change extensions"
            )
        unknown = sorted(set(names) - set(parent_tools))
        if unknown:
            raise SubagentRequestError(
                f"unknown tools {unknown}; a subagent can get only your own tools"
            )
        writes = sorted(n for n in names if is_writing(n, parent_tools))
        if writes and not settings.allow_write:
            raise SubagentRequestError(
                f"{writes} change files or run commands; dynamic subagents are read-only "
                "unless the user sets [subagents] allow_write = true"
            )
    else:
        names = tuple(
            n
            for n in base.tools
            if n not in CHILD_FORBIDDEN
            and (settings.allow_write or not is_writing(n, parent_tools))
        )
    changes["tools"] = names
    changes["unsafe"] = any(is_writing(n, parent_tools) for n in names)

    if "skills" in args and args["skills"] is not None:
        wanted = _string_list(args, "skills", "skill names")
        unknown = sorted(set(wanted) - set(skills))
        if unknown:
            raise SubagentRequestError(
                f"unknown skills {unknown}; available: {', '.join(sorted(skills)) or 'none'}"
            )
        changes["skills"] = wanted

    if "model" in args and args["model"] is not None:
        model = args["model"]
        if not isinstance(model, str) or (model not in ROLES and model not in settings.models):
            allowed = [*ROLES, *settings.models]
            raise SubagentRequestError(
                f"model {model!r} is not allowed; use one of: {', '.join(allowed)}"
            )
        changes["model"] = model

    if "context" in args and args["context"] is not None:
        if not isinstance(args["context"], str):
            raise SubagentRequestError("context must be a string")
        changes["context"] = args["context"].strip()

    return dataclasses.replace(base, **changes), True
