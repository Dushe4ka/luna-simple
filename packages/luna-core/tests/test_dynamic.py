import pytest
from luna_core.dynamic import SubagentRequestError, resolve_spec
from luna_core.events import ToolFinished
from luna_core.loop import Agent
from luna_core.policy import Policy
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session
from luna_core.subagents import (
    BUILTIN_SUBAGENTS,
    READ_ONLY,
    DynamicSettings,
    SubagentRunner,
    SubagentSpec,
)
from luna_core.tools import builtin_tools
from luna_core.tools.base import tool
from luna_core.tools.tasks import task_tool
from luna_core.workspace import Workspace


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


def agent_with(tmp_path, script, child, **runner_kw):
    runner = SubagentRunner(
        dict(BUILTIN_SUBAGENTS), model_for=lambda s: (child, "child"), **runner_kw
    )
    ws = Workspace(tmp_path)
    return Agent(
        FakeProvider(script),
        "parent",
        workspace=ws,
        tools=[*builtin_tools(), task_tool(runner)],
        policy=Policy(workspace=ws, yolo=True),
    )


async def run_turn(agent, tmp_path):
    log = EventLog(tmp_path / "db" / "s.db")
    session = Session.create(log, str(tmp_path), undo=False)
    [e async for e in agent.run(session, "go")]
    return session, log


async def test_task_call_assembles_a_dynamic_subagent(tmp_path):
    child = FakeProvider([FakeProvider.text("done it")])
    call = {"description": "audit", "prompt": "You audit.", "tools": ["grep"], "model": "fast"}
    agent = agent_with(
        tmp_path, [FakeProvider.call("task", call, "t"), FakeProvider.text("ok")], child
    )
    session, log = await run_turn(agent, tmp_path)
    result = session.messages()[2].results[0]
    assert not result.is_error and "done it" in result.text
    assert child.requests[0].system.startswith("You audit.")
    assert [t.name for t in child.requests[0].tools] == ["grep"]
    assert log.children(session.id)[0].meta["dynamic"] is True
    log.close()


async def test_task_call_with_a_forbidden_tool_is_an_error_for_the_model(tmp_path):
    call = {"description": "x", "tools": ["task"]}
    agent = agent_with(
        tmp_path,
        [FakeProvider.call("task", call, "t"), FakeProvider.text("ok")],
        FakeProvider([]),
    )
    session, log = await run_turn(agent, tmp_path)
    result = session.messages()[2].results[0]
    assert result.is_error and "cannot get" in result.text
    assert log.children(session.id) == []
    log.close()


async def test_task_call_resume_reaches_the_runner(tmp_path):
    child = FakeProvider([FakeProvider.text("one"), FakeProvider.text("two")])
    first = {"description": "a", "subagent_type": "researcher"}
    agent = agent_with(
        tmp_path, [FakeProvider.call("task", first, "t1"), FakeProvider.text("ok")], child
    )
    session, log = await run_turn(agent, tmp_path)
    child_id = log.children(session.id)[0].id
    agent.provider = FakeProvider(
        [
            FakeProvider.call("task", {"description": "b", "resume": child_id}, "t2"),
            FakeProvider.text("ok"),
        ]
    )
    [e async for e in agent.run(session, "again")]
    result = session.messages()[-2].results[0]
    assert "two" in result.text and len(log.children(session.id)) == 1
    log.close()


def test_task_schema_lists_the_new_fields():
    runner = SubagentRunner(dict(BUILTIN_SUBAGENTS))
    props = task_tool(runner).spec.parameters["properties"]
    assert {"prompt", "tools", "skills", "model", "max_steps", "context", "resume"} <= set(props)


def _task_details(events):
    return [e.detail for e in events if isinstance(e, ToolFinished) and e.name == "task"]


async def test_task_detail_names_the_assembly_on_run_and_resume(tmp_path):
    child = FakeProvider([FakeProvider.text("one"), FakeProvider.text("two")])
    call = {"description": "audit", "prompt": "You audit.", "tools": ["grep"]}
    agent = agent_with(
        tmp_path, [FakeProvider.call("task", call, "t1"), FakeProvider.text("ok")], child
    )
    log = EventLog(tmp_path / "db" / "s.db")
    session = Session.create(log, str(tmp_path), undo=False)
    expected = "general-purpose (dyn): done · grep · fast"
    events = [e async for e in agent.run(session, "go")]
    assert _task_details(events) == [expected]
    child_id = log.children(session.id)[0].id
    agent.provider = FakeProvider(
        [
            FakeProvider.call("task", {"description": "again", "resume": child_id}, "t2"),
            FakeProvider.text("ok"),
        ]
    )
    events = [e async for e in agent.run(session, "again")]
    assert _task_details(events) == [expected]
    log.close()


def test_settings_max_steps_caps_a_dynamic_call_without_max_steps(parent_tools):
    spec, dynamic = resolve({"prompt": "x"}, parent_tools, max_steps=5)
    assert dynamic and spec.max_steps == 5
