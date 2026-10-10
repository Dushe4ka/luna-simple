import asyncio
from typing import Literal

import pytest
from luna_core.messages import Text
from luna_core.tools.base import Tool, ToolContext, ToolOutput, args_preview, tool


@tool()
def greet(name: str, times: int = 1, mood: Literal["calm", "loud"] = "calm") -> str:
    """Say hello.

    Args:
        name: who to greet.
        times: how many times.
        mood: tone of voice.
    """
    return " ".join([f"hi {name}"] * times)


@tool(name="ctx_echo", mutating=True)
async def echo(ctx: ToolContext, items: list[str], note: str | None = None) -> ToolOutput:
    """Echo items back."""
    return ToolOutput.text(f"{ctx.session_id}:{','.join(items)}:{note}")


def test_schema_from_hints_and_docstring():
    spec = greet.spec
    assert spec.name == "greet"
    assert spec.description == "Say hello."
    props = spec.parameters["properties"]
    assert props["name"] == {"type": "string", "description": "who to greet."}
    assert props["times"] == {"type": "integer", "description": "how many times.", "default": 1}
    assert props["mood"]["enum"] == ["calm", "loud"]
    assert spec.parameters["required"] == ["name"]
    assert spec.parameters["additionalProperties"] is False


def test_ctx_is_injected_and_hidden_from_schema():
    props = echo.spec.parameters["properties"]
    assert "ctx" not in props
    assert props["items"] == {"type": "array", "items": {"type": "string"}}
    assert props["note"] == {"type": "string"}
    assert echo.mutating is True and echo.name == "ctx_echo"


async def test_invoke_sync_and_async(tmp_path):
    ctx = ToolContext(session_id="s1")
    out = await greet.invoke(ctx, {"name": "Luna", "times": 2})
    assert out.content == (Text("hi Luna hi Luna"),) and out.is_error is False
    out = await echo.invoke(ctx, {"items": ["a", "b"]})
    assert out.content == (Text("s1:a,b:None"),)


async def test_bad_arguments_become_an_error_result():
    out = await greet.invoke(ToolContext(), {"times": 2})
    assert out.is_error is True
    assert "name" in out.content[0].text


async def test_exceptions_become_error_results():
    @tool()
    def boom() -> str:
        """Fail."""
        raise RuntimeError("kaput")

    out = await boom.invoke(ToolContext(), {})
    assert out.is_error is True and "kaput" in out.content[0].text


async def test_timeout_is_enforced():
    @tool(timeout=0.05)
    async def slow() -> str:
        """Sleep."""
        await asyncio.sleep(1)
        return "late"

    out = await slow.invoke(ToolContext(), {})
    assert out.is_error is True and "timed out" in out.content[0].text


def test_args_preview():
    assert args_preview({"n": 3, "query": "  погода\n Орёл  "}) == "погода Орёл"
    long = args_preview({"q": "x" * 100})
    assert len(long) == 40 and long.endswith("…")
    assert args_preview({"n": 3}) == ""


def test_a_plain_tool_can_be_built_without_the_decorator():
    async def fn(ctx, args):
        return ToolOutput.text(args["x"])

    t = Tool(
        "raw",
        "Raw tool.",
        {"type": "object", "properties": {"x": {"type": "string"}}},
        fn,
        raw=True,
    )
    assert asyncio.run(t.invoke(ToolContext(), {"x": "ok"})).content == (Text("ok"),)


def test_unsupported_annotation_is_rejected_at_definition():
    with pytest.raises(TypeError, match="unsupported"):

        @tool()
        def bad(x: set) -> str:
            """Bad."""
            return ""
