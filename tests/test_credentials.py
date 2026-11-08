import os
import stat

import pytest

from luna.config.credentials import (
    credentials_path,
    get_api_key,
    mask_key,
    set_api_key,
    unset_api_key,
)
from luna.config.providers import LunaConfigError, ProviderSpec, build_provider


def test_set_get_roundtrip():
    set_api_key("anthropic", "sk-ant-secret")
    assert get_api_key("anthropic") == "sk-ant-secret"


def test_missing_key_is_none():
    assert get_api_key("openai") is None


def test_file_is_chmod_600():
    path = set_api_key("deepseek", "dsk-123")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_unset_removes_key():
    set_api_key("openai", "sk-openai")
    assert unset_api_key("openai") is True
    assert get_api_key("openai") is None
    assert unset_api_key("openai") is False


def test_second_provider_does_not_clobber_first():
    set_api_key("anthropic", "a-key")
    set_api_key("openai", "o-key")
    assert get_api_key("anthropic") == "a-key"
    assert get_api_key("openai") == "o-key"


def test_unknown_provider_rejected():
    with pytest.raises(LunaConfigError):
        set_api_key("grok", "x")


def test_credentials_path_follows_xdg(isolated_config_home):
    assert credentials_path() == isolated_config_home / "luna" / "credentials.toml"


def test_mask_key():
    assert mask_key("sk-ant-abcd1234") == "****1234"


def test_build_provider_reads_stored_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    set_api_key("anthropic", "sk-ant-fromfile")
    provider, _ = build_provider("anthropic", None)  # must not raise about a missing key
    assert provider.api_key == "sk-ant-fromfile"
    # the stored key goes to the adapter only — never into the process env
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_env_var_still_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fromenv")
    set_api_key("anthropic", "sk-ant-fromfile")
    provider, _ = build_provider("anthropic", None)
    assert provider.api_key == "sk-ant-fromenv"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-fromenv"


def test_error_hint_mentions_setup(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LunaConfigError) as exc:
        build_provider("anthropic", None)
    assert "luna setup" in str(exc.value)


def test_set_api_key_accepts_a_provider_from_a_custom_registry():
    custom_registry = {
        "mylocal": ProviderSpec(
            "mylocal", "openai_chat", "local-model", "MYLOCAL_API_KEY", "http://localhost:8000/v1"
        )
    }
    set_api_key("mylocal", "sk-local-test", registry=custom_registry)
    assert get_api_key("mylocal") == "sk-local-test"


def test_set_api_key_still_rejects_unknown_provider_against_a_custom_registry():
    custom_registry = {
        "mylocal": ProviderSpec(
            "mylocal", "openai_chat", "local-model", "MYLOCAL_API_KEY", "http://localhost:8000/v1"
        )
    }
    with pytest.raises(LunaConfigError):
        set_api_key("not-in-either-registry", "sk-test", registry=custom_registry)


def test_unset_api_key_works_with_a_custom_registry():
    custom_registry = {
        "mylocal": ProviderSpec(
            "mylocal", "openai_chat", "local-model", "MYLOCAL_API_KEY", "http://localhost:8000/v1"
        )
    }
    set_api_key("mylocal", "sk-local-test", registry=custom_registry)
    assert unset_api_key("mylocal") is True
    assert get_api_key("mylocal") is None


def test_credentials_file_is_never_created_world_readable(monkeypatch):
    """The key must not sit in a 0644 file until a later chmod (there is none here)."""
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    path = set_api_key("openai", "sk-secret")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# --- the credentials file can never be corrupted or emptied ---------------


@pytest.mark.parametrize(
    "bad", ["a\nb", "a\rb", "a\x00b", "a\x1fb", "a\x7fb", "a b", "a\tb", "ключ"]
)
def test_keys_with_control_chars_whitespace_or_non_ascii_are_refused(bad):
    from luna.config.credentials import BadApiKeyError

    set_api_key("anthropic", "good")
    with pytest.raises(BadApiKeyError):
        set_api_key("openai", bad)
    assert get_api_key("anthropic") == "good"
    assert get_api_key("openai") is None


def test_lone_surrogate_never_empties_the_file():
    from luna.config.credentials import BadApiKeyError

    set_api_key("anthropic", "good")
    with pytest.raises(BadApiKeyError):
        set_api_key("openai", "a\ud800b")
    assert get_api_key("anthropic") == "good"


def test_quote_and_backslash_in_a_key_round_trip():
    set_api_key("anthropic", 'a"b\\c')
    assert get_api_key("anthropic") == 'a"b\\c'


def test_dotted_custom_provider_name_round_trips():
    registry = {"my.llm": ProviderSpec("my.llm", "openai_chat", "m", "MY_LLM_KEY", "http://x/v1")}
    set_api_key("anthropic", "good")
    set_api_key("my.llm", "k1", registry=registry)
    assert get_api_key("my.llm") == "k1"
    assert get_api_key("anthropic") == "good"


@pytest.mark.parametrize("name", ["a]\n[b", 'a"b', "a b", ""])
def test_provider_names_that_could_inject_tables_are_refused(name):
    registry = {name: ProviderSpec(name, "openai_chat", "m", "X_KEY", "http://x/v1")}
    with pytest.raises(LunaConfigError):
        set_api_key(name, "k", registry=registry)
    assert not credentials_path().exists()


def test_a_crash_mid_write_leaves_the_old_file_intact(monkeypatch):
    from luna.config import credentials

    set_api_key("anthropic", "good")

    def boom(*a, **kw):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(credentials.os, "fsync", boom)
        with pytest.raises(OSError):
            set_api_key("openai", "other")
    assert get_api_key("anthropic") == "good"
    assert get_api_key("openai") is None
    assert [
        p.name
        for p in credentials_path().parent.iterdir()
        if "credentials" in p.name and not p.name.endswith(".lock")
    ] == ["credentials.toml"]


def test_concurrent_writers_never_lose_a_key(monkeypatch):
    import threading
    import time

    from luna.config import credentials

    real_load = credentials.load_credentials

    def slow_load(env=None):
        data = real_load(env)
        time.sleep(0.01)  # widen the read-modify-write window
        return data

    patch = monkeypatch.context()
    patch.__enter__().setattr(credentials, "load_credentials", slow_load)
    names = [f"p{i}" for i in range(12)]
    registry = {
        n: ProviderSpec(n, "openai_chat", "m", f"{n.upper()}_KEY", "http://x/v1") for n in names
    }
    threads = [
        threading.Thread(target=set_api_key, args=(n, f"k-{n}"), kwargs={"registry": registry})
        for n in names
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    patch.__exit__(None, None, None)
    assert {n: get_api_key(n) for n in names} == {n: f"k-{n}" for n in names}


def test_a_stow_style_symlink_is_written_through_to_its_target(tmp_path):
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / "credentials.toml"
    target.write_text('[x]\napi_key = "keep"\n')
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    set_api_key("anthropic", "good")
    assert path.is_symlink()
    assert get_api_key("anthropic") == "good" and get_api_key("x") == "keep"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in dotfiles.iterdir()] == ["credentials.toml"]


def test_legacy_nested_tables_are_preserved_not_stringified():
    from luna.config.credentials import load_credentials

    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[my.llm]\napi_key = "k"\n')
    set_api_key("anthropic", "good")
    data = load_credentials()
    assert data["my"] == {"llm": {"api_key": "k"}}
    assert data["anthropic"] == {"api_key": "good"}


def test_writers_stay_serialized_when_the_lock_file_cannot_be_created(monkeypatch):
    from luna_core import filelock

    monkeypatch.setattr(filelock, "_open", lambda path: None)
    test_concurrent_writers_never_lose_a_key(monkeypatch)
