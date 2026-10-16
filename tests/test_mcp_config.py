import json

import pytest

from luna.config.providers import LunaConfigError
from luna.extensions.mcp import MCP_AVAILABLE, add_server, load_mcp_config, remove_server


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


def test_mcp_available_is_a_flag():
    assert isinstance(MCP_AVAILABLE, bool)


def test_invalid_json_is_a_config_error(tmp_path, monkeypatch):
    f = _user_mcp(tmp_path, monkeypatch)
    f.write_text("{not json")
    with pytest.raises(LunaConfigError):
        load_mcp_config(str(tmp_path))
    with pytest.raises(LunaConfigError):
        add_server("x", {"command": "y"}, workdir=str(tmp_path))
