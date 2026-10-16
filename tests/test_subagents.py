import pytest
from luna_core.subagents import READ_ONLY, WRITE_TOOLS, SubagentSpec

from luna.config.providers import LunaConfigError
from luna.extensions.subagents import load_subagents, subagent_files, subagent_summaries


def _write_subagents(tmp_path, body: str):
    d = tmp_path / ".luna"
    d.mkdir(exist_ok=True)
    (d / "subagents.toml").write_text(body)


def test_mutating_subagent_without_unsafe_is_rejected(tmp_path):
    _write_subagents(
        tmp_path,
        '[subagent.impl]\ndescription = "x"\ntools = ["execute", "read_file"]\n',
    )
    with pytest.raises(LunaConfigError, match="unsafe = true"):
        load_subagents(str(tmp_path), on_warn=lambda _l: None)


def test_unsafe_mutating_subagent_loads_and_warns(tmp_path):
    _write_subagents(
        tmp_path,
        '[subagent.impl]\ndescription = "x"\ntools = ["write_file", "read_file"]\nunsafe = true\n',
    )
    warns: list[str] = []
    subs = load_subagents(str(tmp_path), on_warn=warns.append)
    assert isinstance(subs["impl"], SubagentSpec)
    assert subs["impl"].tools == ("write_file", "read_file") and subs["impl"].unsafe
    assert warns and "impl" in warns[0]


def test_builtins_present_and_read_only_ones_are_safe(tmp_path):
    subs = load_subagents(str(tmp_path))
    assert {"researcher", "reviewer", "general-purpose"} <= set(subs)
    for name in ("researcher", "reviewer"):
        assert not set(subs[name].tools) & set(WRITE_TOOLS) and not subs[name].writes


def test_user_subagent_without_tools_key_is_read_only(tmp_path):
    _write_subagents(tmp_path, '[subagent.helper]\ndescription = "help"\nprompt = "you help"\n')
    helper = load_subagents(str(tmp_path))["helper"]
    assert helper.tools == READ_ONLY
    assert (helper.description, helper.prompt) == ("help", "you help")


def test_unsafe_string_value_does_not_count_as_consent(tmp_path):
    _write_subagents(
        tmp_path,
        '[subagent.x]\ndescription = "x"\ntools = ["execute", "read_file"]\nunsafe = "yes"\n',
    )
    with pytest.raises(LunaConfigError, match="unsafe = true"):
        load_subagents(str(tmp_path), on_warn=lambda _l: None)


def test_user_subagent_parsed(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    (cfg / "subagents.toml").write_text(
        '[subagent.docs]\ndescription = "write docs"\nprompt = "You write docs."\n'
        'tools = ["read_file", "write_file"]\nunsafe = true\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    assert ("docs", "write docs") in subagent_summaries(str(tmp_path))


def test_project_file_overrides_user_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    user, project = subagent_files(str(tmp_path))
    assert user == tmp_path / ".config" / "luna" / "subagents.toml"
    user.parent.mkdir(parents=True)
    user.write_text('[subagent.a]\ndescription = "user"\n')
    _write_subagents(tmp_path, '[subagent.a]\ndescription = "project"\n')
    assert load_subagents(str(tmp_path))["a"].description == "project"


def test_bad_tool_name_rejected(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    (cfg / "subagents.toml").write_text('[subagent.x]\ndescription = "d"\ntools = ["frobnicate"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    with pytest.raises(LunaConfigError, match="unknown tools"):
        load_subagents(str(tmp_path))
