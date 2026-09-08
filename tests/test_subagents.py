import pytest

from luna.providers import LunaConfigError
from luna.subagents import (
    _MUTATING_TOOLS,
    _SAFE_TOOLS,
    BUILTIN_SUBAGENTS,
    load_subagents,
    subagent_summaries,
)


def _write_subagents(tmp_path, body: str):
    d = tmp_path / ".luna"
    d.mkdir(exist_ok=True)
    (d / "subagents.toml").write_text(body)


def test_mutating_subagent_without_unsafe_is_rejected(tmp_path):
    _write_subagents(tmp_path, '[subagent.impl]\ndescription = "x"\ntools = ["execute"]\n')
    with pytest.raises(LunaConfigError, match="unsafe"):
        load_subagents(str(tmp_path))


def test_unsafe_mutating_subagent_loads_and_warns(tmp_path):
    _write_subagents(
        tmp_path,
        '[subagent.impl]\ndescription = "x"\ntools = ["write_file", "read_file"]\nunsafe = true\n',
    )
    warns: list[str] = []
    subs = load_subagents(str(tmp_path), on_warn=warns.append)
    names = {s["name"] for s in subs}
    assert "impl" in names
    assert warns and "impl" in warns[0]


def test_guard_is_first_middleware_on_every_subagent(tmp_path):
    sentinel = object()
    subs = load_subagents(str(tmp_path), guard=sentinel)
    for s in subs:
        assert s["middleware"][0] is sentinel


def test_builtins_are_safe_only():
    assert _MUTATING_TOOLS.isdisjoint(_SAFE_TOOLS)
    subs = load_subagents(".")
    assert {s["name"] for s in subs} >= {"researcher", "reviewer"}


def test_user_subagent_without_tools_key_is_restricted(tmp_path):
    from deepagents.middleware import FilesystemMiddleware

    _write_subagents(
        tmp_path,
        '[subagent.helper]\ndescription = "help"\nprompt = "you help"\n',
    )
    subs = load_subagents(str(tmp_path))
    helper = next(s for s in subs if s["name"] == "helper")
    fs = [m for m in helper["middleware"] if isinstance(m, FilesystemMiddleware)]
    assert fs, "missing tools key must fall back to a restricted FilesystemMiddleware"


def test_unsafe_string_value_does_not_count_as_consent(tmp_path):
    _write_subagents(
        tmp_path,
        '[subagent.x]\ndescription = "x"\ntools = ["execute"]\nunsafe = "yes"\n',
    )
    with pytest.raises(LunaConfigError, match="unsafe"):
        load_subagents(str(tmp_path))


def test_builtins_present():
    names = {s["name"] for s in BUILTIN_SUBAGENTS}
    assert {"researcher", "reviewer"} <= names


def test_user_subagent_parsed(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    (cfg / "subagents.toml").write_text(
        '[subagent.docs]\ndescription = "write docs"\nprompt = "You write docs."\n'
        'tools = ["read_file", "write_file"]\nunsafe = true\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    names = [n for n, _ in subagent_summaries(str(tmp_path))]
    assert "docs" in names


def test_fast_model_applied_to_builtins(tmp_path):
    subs = load_subagents(str(tmp_path), fast_model="anthropic:claude-haiku-4-5")
    by_name = {s["name"]: s for s in subs}
    assert by_name["researcher"].get("model") == "anthropic:claude-haiku-4-5"


def test_bad_tool_name_rejected(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    (cfg / "subagents.toml").write_text('[subagent.x]\ndescription = "d"\ntools = ["frobnicate"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    with pytest.raises(LunaConfigError):
        load_subagents(str(tmp_path))
