import pytest
from luna_core.dynamic import SubagentRequestError, resolve_spec
from luna_core.subagents import BUILTIN_SUBAGENTS, READ_ONLY, DynamicSettings, SubagentSpec
from luna_core.tools import builtin_tools
from luna_core.tools.base import tool


@tool(mutating=True)
async def mcp__db__write(sql: str) -> str:
    """Write to the db."""
    return "ok"


@tool()
async def mcp__db__query(sql: str) -> str:
    """Query the db."""
    return "rows"


@pytest.fixture
def parent_tools():
    tools = {t.name: t for t in builtin_tools()}
    for extra in (mcp__db__write, mcp__db__query):
        tools[extra.name] = extra
    tools["task"] = object()  # the parent has it; a child must never get it
    return tools


def resolve(args, parent_tools, *, base="researcher", skills=("sql",), **settings):
    return resolve_spec(
        BUILTIN_SUBAGENTS[base],
        args,
        parent_tools=parent_tools,
        skills=skills,
        settings=DynamicSettings(**settings),
    )


def test_plain_call_returns_the_base_unchanged(parent_tools):
    spec, dynamic = resolve({"description": "x"}, parent_tools)
    assert spec is BUILTIN_SUBAGENTS["researcher"] and dynamic is False


def test_max_steps_alone_is_not_dynamic(parent_tools):
    spec, dynamic = resolve({"max_steps": 5}, parent_tools)
    assert spec.max_steps == 5 and dynamic is False


def test_fields_override_the_base(parent_tools):
    spec, dynamic = resolve(
        {
            "prompt": "You audit SQL.",
            "tools": ["read_file", "mcp__db__query"],
            "skills": ["sql"],
            "model": "fast",
            "context": "postgres 16",
            "max_steps": 12,
        },
        parent_tools,
    )
    assert dynamic is True
    assert spec.prompt == "You audit SQL."
    assert spec.tools == ("read_file", "mcp__db__query")
    assert (spec.skills, spec.model, spec.context, spec.max_steps) == (
        ("sql",),
        "fast",
        "postgres 16",
        12,
    )
    assert spec.name == "researcher" and not spec.unsafe


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"tools": ["read_file", "task"]}, "cannot get"),
        ({"tools": ["teleport"]}, "unknown tools"),
        ({"tools": ["edit_file"]}, "allow_write"),
        ({"tools": ["mcp__db__write"]}, "allow_write"),
        ({"tools": []}, "at least one tool"),
        ({"tools": "read_file"}, "list of tool names"),
        ({"prompt": "   "}, "prompt must be a non-empty string"),
        ({"skills": ["nope"]}, "unknown skills"),
        ({"model": "gpt-9"}, "not allowed"),
        ({"max_steps": 31}, "between 1 and 30"),
        ({"max_steps": True}, "between 1 and 30"),
        ({"context": 5}, "context must be a string"),
    ],
)
def test_bad_requests_are_explained(parent_tools, args, message):
    with pytest.raises(SubagentRequestError, match=message):
        resolve(args, parent_tools)


def test_write_tools_need_allow_write(parent_tools):
    spec, _ = resolve({"tools": ["read_file", "edit_file"]}, parent_tools, allow_write=True)
    assert spec.tools == ("read_file", "edit_file") and spec.unsafe and spec.writes


def test_allowed_model_ids_come_from_settings(parent_tools):
    spec, _ = resolve({"model": "my-model-id"}, parent_tools, models=("my-model-id",))
    assert spec.model == "my-model-id"


def test_dynamic_off_rejects_dynamic_fields_but_not_plain_calls(parent_tools):
    with pytest.raises(SubagentRequestError, match="disabled"):
        resolve({"prompt": "p"}, parent_tools, enabled=False)
    assert resolve({"description": "x"}, parent_tools, enabled=False)[1] is False


def test_writer_base_is_reduced_to_reading_without_allow_write(parent_tools):
    spec, dynamic = resolve({"prompt": "Summarise."}, parent_tools, base="general-purpose")
    assert dynamic and set(spec.tools) == {*READ_ONLY, "write_todos"}
    assert not spec.unsafe and not spec.writes


def test_writer_base_keeps_its_tools_with_allow_write(parent_tools):
    spec, _ = resolve({"prompt": "Fix it."}, parent_tools, base="general-purpose", allow_write=True)
    assert "edit_file" in spec.tools and spec.unsafe


def test_duplicate_tools_are_collapsed_in_order(parent_tools):
    spec, _ = resolve({"tools": ["grep", "read_file", "grep"]}, parent_tools)
    assert spec.tools == ("grep", "read_file")


def test_custom_base_is_kept_as_the_name(parent_tools):
    base = SubagentSpec("db", "db work", "p", ("mcp__db__query",))
    spec, _ = resolve_spec(
        base, {"context": "c"}, parent_tools=parent_tools, skills=(), settings=DynamicSettings()
    )
    assert spec.name == "db" and spec.tools == ("mcp__db__query",)
