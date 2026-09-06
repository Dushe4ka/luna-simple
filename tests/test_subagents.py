import pytest

from luna.providers import LunaConfigError
from luna.subagents import BUILTIN_SUBAGENTS, load_subagents, subagent_summaries


def test_builtins_present():
    names = {s["name"] for s in BUILTIN_SUBAGENTS}
    assert {"researcher", "reviewer"} <= names


def test_user_subagent_parsed(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    (cfg / "subagents.toml").write_text(
        '[subagent.docs]\ndescription = "write docs"\nprompt = "You write docs."\n'
        'tools = ["read_file", "write_file"]\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    names = [n for n, _ in subagent_summaries(str(tmp_path))]
    assert "docs" in names


def test_bad_tool_name_rejected(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    (cfg / "subagents.toml").write_text('[subagent.x]\ndescription = "d"\ntools = ["frobnicate"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    with pytest.raises(LunaConfigError):
        load_subagents(str(tmp_path))
