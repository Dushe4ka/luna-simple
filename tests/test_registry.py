import pytest

from luna.config.providers import LunaConfigError
from luna.extensions.registry import known_mcp, resolve_mcp, resolve_skill


def test_builtin_mcp_lookup():
    spec = resolve_mcp("filesystem")
    assert spec["command"] == "npx"
    assert "@modelcontextprotocol/server-filesystem" in spec["args"]


def test_builtin_mcp_tavily_web_search_expands_its_api_key_env_var():
    """Web search is Tavily's own official MCP server (tavily-mcp), the
    same way OpenCode — a comparable terminal coding agent, checked
    directly rather than assumed — points users at it rather than bundling
    a hosted search backend of its own. Its API key follows the same
    ${ENV} pattern the existing `github` entry already uses.
    """
    spec = resolve_mcp("tavily")
    assert spec["command"] == "npx"
    assert "tavily-mcp" in spec["args"]
    assert spec["env"]["TAVILY_API_KEY"] == "${TAVILY_API_KEY}"


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
