import pytest

from luna.config.providers import LunaConfigError
from luna.extensions.registry import known_mcp, resolve_mcp, resolve_skill


def test_builtin_mcp_lookup():
    spec = resolve_mcp("filesystem")
    assert spec["command"] == "npx"
    assert "@modelcontextprotocol/server-filesystem" in spec["args"]


def test_builtin_skill_lookup():
    assert resolve_skill("pdf")["repo"] == "anthropics/skills"


def test_unknown_raises_and_lists():
    with pytest.raises(LunaConfigError) as e:
        resolve_mcp("nope")
    assert "filesystem" in str(e.value)


def test_user_registry_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    (cfg / "registry.toml").write_text(
        '[mcp.filesystem]\ncommand = "uvx"\nargs = ["my-fs"]\n'
        '[mcp.custom]\ncommand = "node"\nargs = ["server.js"]\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    assert resolve_mcp("filesystem")["command"] == "uvx"
    assert resolve_mcp("custom")["args"] == ["server.js"]
    assert "custom" in known_mcp()
