"""The tool API: specs for providers, invocation with validation and timeouts."""

from __future__ import annotations

import asyncio
import inspect
import re
import types
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Union, get_args, get_origin

from luna_core.messages import Image, Text


@dataclass(frozen=True)
class ToolSpec:
    """What a provider sees: name, description, JSON Schema of the arguments."""

    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolOutput:
    """A tool's result: content for the model, a short detail for the UI."""

    content: tuple[Text | Image, ...]
    is_error: bool = False
    detail: str = ""

    @classmethod
    def text(cls, text: str, detail: str = "") -> ToolOutput:
        """Build a successful text result."""
        return cls((Text(text),), False, detail)

    @classmethod
    def error(cls, text: str) -> ToolOutput:
        """Build an error result (the model sees it; the turn continues)."""
        return cls((Text(text),), True, text.splitlines()[0][:120] if text else "")


@dataclass
class ToolContext:
    """Everything a tool may use while it runs.

    ``services`` carries app- or runtime-provided objects by name (for
    example ``"background"``, ``"subagents"``, ``"policy"``); tools look them
    up instead of importing global singletons.
    """

    workspace: Any = None
    sandbox: Any = None
    session_id: str = ""
    call_id: str = ""
    emit: Callable[[object], None] = lambda event: None
    state: dict = field(default_factory=dict)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    services: dict = field(default_factory=dict)


ToolFn = Callable[..., Any]


class Tool:
    """A callable tool with a JSON-Schema spec.

    Built with :func:`tool` from a typed function, or directly with
    ``raw=True`` and ``fn(ctx, args) -> ToolOutput`` for dynamic tools such
    as MCP ones.
    """

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict,
        fn: ToolFn,
        *,
        mutating: bool = False,
        timeout: float | None = None,
        raw: bool = False,
        wants_ctx: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.mutating = mutating
        self.timeout = timeout
        self._fn = fn
        self._raw = raw
        self._wants_ctx = wants_ctx

    @property
    def spec(self) -> ToolSpec:
        """The provider-facing description of this tool."""
        return ToolSpec(self.name, self.description, self.parameters)

    async def invoke(self, ctx: ToolContext, args: dict) -> ToolOutput:
        """Run the tool; every failure becomes an error result, never an exception."""
        try:
            coro = self._call(ctx, args)
            result = await asyncio.wait_for(coro, self.timeout) if self.timeout else await coro
        except TimeoutError:
            return ToolOutput.error(f"{self.name} timed out after {self.timeout:g}s")
        except asyncio.CancelledError:
            raise
        except _ArgError as exc:
            return ToolOutput.error(f"invalid arguments for {self.name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - tool failures are results for the model
            return ToolOutput.error(f"{self.name} failed: {type(exc).__name__}: {exc}")
        return _to_output(result)

    async def _call(self, ctx: ToolContext, args: dict) -> Any:
        if self._raw:
            return await _maybe_await(self._fn(ctx, args))
        kwargs = _bind(self.parameters, args)
        if self._wants_ctx:
            return await _maybe_await(self._fn(ctx, **kwargs))
        return await _maybe_await(self._fn(**kwargs))


class _ArgError(Exception):
    pass


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _to_output(result: Any) -> ToolOutput:
    if isinstance(result, ToolOutput):
        return result
    if isinstance(result, str):
        return ToolOutput.text(result)
    if isinstance(result, (Text, Image)):
        return ToolOutput((result,))
    return ToolOutput.text(str(result))


def _bind(schema: dict, args: dict) -> dict:
    props = schema.get("properties", {})
    unknown = set(args) - set(props)
    if unknown:
        raise _ArgError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    missing = [name for name in schema.get("required", []) if name not in args]
    if missing:
        raise _ArgError(f"missing required argument(s): {', '.join(missing)}")
    for name, value in args.items():
        _check(name, props[name], value)
    return dict(args)


_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _check(name: str, prop: dict, value: Any) -> None:
    if value is None and prop.get("nullable", True):
        return
    expected = prop.get("type")
    if expected is None:
        return
    py = _JSON_TYPES[expected]
    if expected in ("integer", "number") and isinstance(value, bool):
        raise _ArgError(f"{name} must be {expected}")
    if not isinstance(value, py):
        raise _ArgError(f"{name} must be {expected}")
    if "enum" in prop and value not in prop["enum"]:
        raise _ArgError(f"{name} must be one of {prop['enum']}")


_PRIMITIVES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _schema_for(annotation: Any) -> dict:
    origin = get_origin(annotation)
    if annotation in _PRIMITIVES:
        return {"type": _PRIMITIVES[annotation]}
    if annotation in (dict, typing.Dict) or origin is dict:  # noqa: UP006
        return {"type": "object"}
    if origin in (list, typing.List):  # noqa: UP006
        (item,) = get_args(annotation) or (str,)
        return {"type": "array", "items": _schema_for(item)}
    if origin is Literal:
        values = list(get_args(annotation))
        return {"type": _PRIMITIVES[type(values[0])], "enum": values}
    if origin in (Union, types.UnionType):
        inner = [a for a in get_args(annotation) if a is not type(None)]
        if len(inner) == 1:
            return _schema_for(inner[0])
    raise TypeError(f"unsupported parameter annotation: {annotation!r}")


_ARGS_SECTION = re.compile(r"^\s*Args:\s*$", re.MULTILINE)
_ARG_LINE = re.compile(r"^\s{2,}(\w+):\s*(.+)$")


def _parse_doc(doc: str) -> tuple[str, dict[str, str]]:
    doc = inspect.cleandoc(doc or "")
    match = _ARGS_SECTION.search(doc)
    summary = (doc[: match.start()] if match else doc).strip()
    params: dict[str, str] = {}
    if match:
        current = None
        for line in doc[match.end() :].splitlines():
            arg = _ARG_LINE.match(line)
            if arg:
                current = arg.group(1)
                params[current] = arg.group(2).strip()
            elif current and line.strip() and line.startswith(" " * 6):
                params[current] += " " + line.strip()
            elif line.strip() and not line.startswith(" "):
                break
    return summary, params


def tool(
    name: str | None = None, *, mutating: bool = False, timeout: float | None = None
) -> Callable[[ToolFn], Tool]:
    """Turn a typed function into a :class:`Tool`.

    The first parameter, if named ``ctx``, receives the :class:`ToolContext`.
    The summary of the docstring becomes the description; an ``Args:``
    section documents parameters.
    """

    def deco(fn: ToolFn) -> Tool:
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn)
        summary, docs = _parse_doc(fn.__doc__ or "")
        props: dict[str, dict] = {}
        required: list[str] = []
        wants_ctx = False
        for pname, param in sig.parameters.items():
            if pname == "ctx":
                wants_ctx = True
                continue
            prop = _schema_for(hints.get(pname, str))
            if pname in docs:
                prop["description"] = docs[pname]
            if param.default is inspect.Parameter.empty:
                required.append(pname)
            elif param.default is not None:
                prop["default"] = param.default
            props[pname] = prop
        schema = {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        }
        return Tool(
            name or fn.__name__,
            summary,
            schema,
            fn,
            mutating=mutating,
            timeout=timeout,
            wants_ctx=wants_ctx,
        )

    return deco


def args_preview(args: dict, limit: int = 40) -> str:
    """Return the first non-empty string argument, whitespace-collapsed and truncated."""
    for value in args.values():
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= limit else text[: limit - 1] + "…"
    return ""


AsyncToolFn = Callable[[ToolContext, dict], Awaitable[ToolOutput]]
