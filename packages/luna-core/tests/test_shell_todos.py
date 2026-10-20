import asyncio

from luna_core.events import TodosUpdated
from luna_core.sandbox import LocalSandbox
from luna_core.tools import builtin_tools
from luna_core.tools.base import ToolContext
from luna_core.tools.shell import execute
from luna_core.tools.todos import write_todos
from luna_core.workspace import Workspace


def make_ctx(tmp_path, events=None):
    return ToolContext(
        workspace=Workspace(tmp_path),
        sandbox=LocalSandbox(),
        emit=(events.append if events is not None else lambda e: None),
    )


async def test_execute_reports_exit_code(tmp_path):
    ctx = make_ctx(tmp_path)
    ok = await execute.invoke(ctx, {"command": "echo hi"})
    assert (
        not ok.is_error
        and ok.content[0].text.strip().endswith("[exit code 0]")
        and "hi" in ok.content[0].text
    )
    bad = await execute.invoke(ctx, {"command": "echo no >&2; exit 4"})
    assert bad.is_error and "[exit code 4]" in bad.content[0].text and "no" in bad.content[0].text


async def test_execute_timeout_is_capped_and_reported(tmp_path):
    out = await execute.invoke(make_ctx(tmp_path), {"command": "sleep 5", "timeout": 1})
    assert out.is_error and "timed out after 1s" in out.content[0].text
    assert execute.parameters["properties"]["timeout"]["default"] == 120


async def test_execute_honours_the_cancel_event(tmp_path):
    ctx = make_ctx(tmp_path)
    task = asyncio.create_task(execute.invoke(ctx, {"command": "sleep 30"}))
    await asyncio.sleep(0.2)
    ctx.cancel.set()
    out = await asyncio.wait_for(task, 5)
    assert out.is_error and "cancelled" in out.content[0].text


async def test_write_todos_validates_stores_and_emits(tmp_path):
    events = []
    ctx = make_ctx(tmp_path, events)
    todos = [
        {"content": "read code", "status": "completed"},
        {"content": "fix bug", "status": "in_progress"},
    ]
    out = await write_todos.invoke(ctx, {"todos": todos})
    assert not out.is_error and ctx.state["todos"] == todos
    assert events == [TodosUpdated(todos)]
    bad = await write_todos.invoke(ctx, {"todos": [{"content": "x", "status": "maybe"}]})
    assert bad.is_error and "status" in bad.content[0].text


def test_builtin_tool_names():
    assert [t.name for t in builtin_tools()] == [
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
        "write_todos",
    ]


def test_builtin_schemas_fit_the_token_budget():
    import json

    size = sum(
        len(json.dumps({"name": t.name, "description": t.description, "parameters": t.parameters}))
        for t in builtin_tools()
    )
    assert size / 4 < 2000  # ~4 chars per token; spec target ≤ 2k tokens for built-ins
