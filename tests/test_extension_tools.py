from luna_core.events import ReloadRequested
from luna_core.tools.base import ToolContext
from luna_core.workspace import Workspace

from luna.extensions.extension_tools import EXTENSION_APPROVALS, manage_mcp, manage_skills
from luna.extensions.mcp import load_mcp_config


def _ctx(root, events: list) -> ToolContext:
    return ToolContext(workspace=Workspace(root), emit=events.append)


async def test_manage_mcp_add_from_registry_requests_a_reload(tmp_path):
    events: list = []
    out = await manage_mcp.invoke(_ctx(tmp_path, events), {"action": "add", "name": "filesystem"})
    assert not out.is_error and "filesystem" in out.content[0].text
    assert "filesystem" in load_mcp_config(str(tmp_path))
    assert any(isinstance(e, ReloadRequested) for e in events)


async def test_manage_mcp_add_explicit_command(tmp_path):
    args = {"action": "add", "name": "custom", "command": "node", "command_args": ["s.js"]}
    await manage_mcp.invoke(_ctx(tmp_path, []), args)
    assert load_mcp_config(str(tmp_path))["custom"]["command"] == "node"


async def test_manage_mcp_remove_only_reloads_when_something_changed(tmp_path):
    events: list = []
    out = await manage_mcp.invoke(_ctx(tmp_path, events), {"action": "remove", "name": "nope"})
    assert "was not configured" in out.content[0].text and events == []
    await manage_mcp.invoke(_ctx(tmp_path, []), {"action": "add", "name": "filesystem"})
    await manage_mcp.invoke(_ctx(tmp_path, events), {"action": "remove", "name": "filesystem"})
    assert len(events) == 1 and "filesystem" not in load_mcp_config(str(tmp_path))


async def test_manage_mcp_list_does_not_reload(tmp_path):
    events: list = []
    out = await manage_mcp.invoke(_ctx(tmp_path, events), {"action": "list"})
    assert "filesystem" in out.content[0].text and events == []


async def test_manage_mcp_needs_a_name(tmp_path):
    out = await manage_mcp.invoke(_ctx(tmp_path, []), {"action": "add"})
    assert out.is_error


async def test_manage_skills_list(tmp_path):
    out = await manage_skills.invoke(_ctx(tmp_path, []), {"action": "list"})
    assert "known:" in out.content[0].text


async def test_manage_skills_remove_missing_is_an_error(tmp_path):
    events: list = []
    out = await manage_skills.invoke(_ctx(tmp_path, events), {"action": "remove", "name": "x"})
    assert out.is_error and events == []


def test_extension_tools_are_mutating_and_approval_gated():
    assert manage_mcp.mutating and manage_skills.mutating
    assert EXTENSION_APPROVALS == {"manage_mcp", "manage_skills", "remember", "save_skill"}
