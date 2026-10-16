"""TEMPORARY: extension wiring of the legacy REPL/server agent.

Deleted together with ``luna/core/_legacy_agent.py`` (Tasks 29-32).
"""

from deepagents.backends import LocalShellBackend
from deepagents.middleware import FilesystemMiddleware

from luna.core._legacy_agent import (
    EXTENSION_INTERRUPTS,
    _manage_mcp,
    _manage_skills,
    _remember,
    _save_skill,
    _subagents,
    load_mcp_tools,
    to_connections,
)
from luna.extensions.mcp import load_mcp_config


def _backend(tmp_path):
    return LocalShellBackend(root_dir=str(tmp_path), virtual_mode=True, inherit_env=True)


# -- MCP ----------------------------------------------------------------------


def test_translation_shapes():
    conns = to_connections(
        {
            "a": {"command": "npx", "args": ["x"], "env": {"T": "1"}},
            "b": {"type": "http", "url": "https://h/mcp", "headers": {"A": "b"}},
            "c": {"type": "sse", "url": "https://h/sse"},
        }
    )
    assert conns["a"] == {"transport": "stdio", "command": "npx", "args": ["x"], "env": {"T": "1"}}
    assert conns["b"]["transport"] == "streamable_http" and conns["b"]["headers"] == {"A": "b"}
    assert conns["c"]["transport"] == "sse"


def test_translation_rejects_a_spec_without_command_or_url():
    import pytest

    from luna.config.providers import LunaConfigError

    with pytest.raises(LunaConfigError):
        to_connections({"x": {"type": "http"}})


def test_load_tools_without_adapter_returns_empty(monkeypatch):
    import luna.core._legacy_agent as legacy

    monkeypatch.setattr(legacy, "LC_MCP_AVAILABLE", False)
    warned: list[str] = []
    assert load_mcp_tools({"x": {}}, on_warn=warned.append) == []
    assert warned and "luna-simple[mcp]" in warned[0]


def test_load_tools_with_no_connections_is_empty():
    assert load_mcp_tools({}, on_warn=lambda _m: None) == []


# -- subagents ----------------------------------------------------------------


def test_guard_is_first_middleware_on_every_subagent(tmp_path):
    sentinel = object()
    subs = _subagents(str(tmp_path), None, sentinel, _backend(tmp_path), None)
    assert {s["name"] for s in subs} >= {"researcher", "reviewer"}
    assert "general-purpose" not in {s["name"] for s in subs}  # deepagents brings its own
    for s in subs:
        assert s["middleware"][0] is sentinel


def test_fast_model_applied_to_builtins(tmp_path):
    subs = _subagents(
        str(tmp_path), "anthropic:claude-haiku-4-5", object(), _backend(tmp_path), None
    )
    by_name = {s["name"]: s for s in subs}
    assert by_name["researcher"].get("model") == "anthropic:claude-haiku-4-5"


def test_user_subagent_without_tools_key_is_restricted(tmp_path):
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "subagents.toml").write_text(
        '[subagent.helper]\ndescription = "help"\nprompt = "you help"\nmodel = "own"\n'
    )
    subs = _subagents(str(tmp_path), "fast", object(), _backend(tmp_path), None)
    helper = next(s for s in subs if s["name"] == "helper")
    assert helper["model"] == "own"  # an explicit model beats fast_model
    fs = [m for m in helper["middleware"] if isinstance(m, FilesystemMiddleware)]
    assert fs, "missing tools key must fall back to a restricted FilesystemMiddleware"


# -- extension tools ----------------------------------------------------------


def test_interrupts_registered():
    assert EXTENSION_INTERRUPTS == {
        "manage_mcp": True,
        "manage_skills": True,
        "remember": True,
        "save_skill": True,
    }


def test_manage_mcp_add_from_registry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = _manage_mcp.invoke({"action": "add", "name": "filesystem"})
    assert "/reload" in out
    assert "filesystem" in load_mcp_config(str(tmp_path))


def test_manage_mcp_add_explicit_command_and_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _manage_mcp.invoke(
        {"action": "add", "name": "custom", "command": "node", "command_args": ["s.js"]}
    )
    assert load_mcp_config(str(tmp_path))["custom"]["command"] == "node"
    assert "custom" in _manage_mcp.invoke({"action": "list"})


def test_manage_skills_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "known:" in _manage_skills.invoke({"action": "list"})


def test_remember_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = _remember.invoke({"kind": "decisions", "topic": "db", "note": "chose sqlite"})
    assert "decisions.md" in out
    assert "chose sqlite" in (tmp_path / ".luna" / "memory" / "decisions.md").read_text()


def test_save_skill_writes_and_lists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = _save_skill.invoke(
        {"name": "fix-flaky-retry", "description": "retries with backoff", "body": "1. Retry.\n"}
    )
    assert "/reload" in out
    assert (tmp_path / ".luna" / "skills" / "fix-flaky-retry" / "SKILL.md").is_file()


def test_save_skill_reports_validation_errors_as_a_string(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("../escape", "a" * 300):
        out = _save_skill.invoke({"name": name, "description": "d", "body": "b"})
        assert isinstance(out, str) and "not a valid skill name" in out


def test_save_skill_reports_replaced_when_name_already_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = _save_skill.invoke({"name": "x", "description": "d1", "body": "b1"})
    assert first.startswith("saved skill")
    second = _save_skill.invoke({"name": "x", "description": "d2", "body": "b2"})
    assert second.startswith("replaced skill")
