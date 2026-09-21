from luna.extensions.extension_tools import (
    EXTENSION_INTERRUPTS,
    manage_mcp,
    manage_skills,
    remember,
    save_skill,
)
from luna.extensions.mcp import load_mcp_config


def test_manage_mcp_add_from_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    out = manage_mcp.invoke({"action": "add", "name": "filesystem"})
    assert "/reload" in out
    assert "filesystem" in load_mcp_config(str(tmp_path))


def test_manage_mcp_add_explicit_command(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    manage_mcp.invoke(
        {"action": "add", "name": "custom", "command": "node", "command_args": ["s.js"]}
    )
    assert load_mcp_config(str(tmp_path))["custom"]["command"] == "node"


def test_manage_mcp_list(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    assert "filesystem" in manage_mcp.invoke({"action": "list"})


def test_manage_skills_list(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    assert "known:" in manage_skills.invoke({"action": "list"})


def test_interrupts_registered():
    assert EXTENSION_INTERRUPTS == {
        "manage_mcp": True,
        "manage_skills": True,
        "remember": True,
        "save_skill": True,
    }


def test_remember_is_interrupted():
    assert EXTENSION_INTERRUPTS.get("remember") is True


def test_remember_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = remember.invoke({"kind": "decisions", "topic": "db", "note": "chose sqlite"})
    assert "decisions.md" in out
    assert "chose sqlite" in (tmp_path / ".luna" / "memory" / "decisions.md").read_text()


def test_save_skill_writes_and_lists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    out = save_skill.invoke(
        {
            "name": "fix-flaky-retry",
            "description": "retries a flaky step with backoff",
            "body": "1. Detect the flaky step.\n2. Retry with backoff.\n",
        }
    )
    assert "/reload" in out
    assert (tmp_path / ".luna" / "skills" / "fix-flaky-retry" / "SKILL.md").is_file()


def test_save_skill_reports_validation_errors_as_a_string(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    out = save_skill.invoke({"name": "../escape", "description": "d", "body": "b"})
    assert "not a valid skill name" in out
