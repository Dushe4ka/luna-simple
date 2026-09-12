import json

from luna.extensions.mcp import (
    add_server,
    load_mcp_config,
    remove_server,
    to_connections,
)


def _user_mcp(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    return cfg / "mcp.json"


def test_env_substitution_and_merge(tmp_path, monkeypatch):
    f = _user_mcp(tmp_path, monkeypatch)
    monkeypatch.setenv("GH", "tok123")
    f.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "gh": {
                        "command": "npx",
                        "args": ["-y", "srv"],
                        "env": {"T": "${GH}"},
                    }
                }
            }
        )
    )
    cfg = load_mcp_config(str(tmp_path))
    assert cfg["gh"]["env"]["T"] == "tok123"


def test_translation_shapes():
    conns = to_connections(
        {
            "a": {"command": "npx", "args": ["x"]},
            "b": {"type": "http", "url": "https://h/mcp"},
            "c": {"type": "sse", "url": "https://h/sse"},
        }
    )
    assert conns["a"]["transport"] == "stdio"
    assert conns["b"]["transport"] == "streamable_http"
    assert conns["c"]["transport"] == "sse"


def test_add_remove_roundtrip(tmp_path, monkeypatch):
    _user_mcp(tmp_path, monkeypatch)
    add_server("fs", {"command": "npx", "args": ["-y", "srv-fs"]}, workdir=str(tmp_path))
    assert "fs" in load_mcp_config(str(tmp_path))
    assert remove_server("fs", workdir=str(tmp_path)) is True
    assert "fs" not in load_mcp_config(str(tmp_path))


def test_project_scope_wins(tmp_path, monkeypatch):
    _user_mcp(tmp_path, monkeypatch)
    add_server("s", {"command": "u", "args": ["a"]}, workdir=str(tmp_path))
    add_server("s", {"command": "p", "args": ["b"]}, project=True, workdir=str(tmp_path))
    assert load_mcp_config(str(tmp_path))["s"]["command"] == "p"


def test_load_tools_without_adapter_returns_empty(monkeypatch):
    import luna.extensions.mcp as m

    monkeypatch.setattr(m, "MCP_AVAILABLE", False)
    warned = []
    assert m.load_mcp_tools({"x": {}}, on_warn=warned.append) == []
    assert warned and "luna-simple[mcp]" in warned[0]
