import os
import stat

import pytest

from luna.credentials import (
    credentials_path,
    get_api_key,
    mask_key,
    set_api_key,
    unset_api_key,
)
from luna.providers import LunaConfigError, build_model


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


def test_build_model_reads_stored_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    set_api_key("anthropic", "sk-ant-fromfile")
    build_model("anthropic")  # must not raise about a missing key
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-fromfile"


def test_env_var_still_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fromenv")
    set_api_key("anthropic", "sk-ant-fromfile")
    build_model("anthropic")
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-fromenv"


def test_error_hint_mentions_setup(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LunaConfigError) as exc:
        build_model("anthropic")
    assert "luna setup" in str(exc.value)
