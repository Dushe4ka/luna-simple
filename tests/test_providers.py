import pytest
from luna_core.providers.anthropic import AnthropicMessages
from luna_core.providers.openai_chat import OpenAIChat

from luna.config.providers import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    LunaConfigError,
    ProviderSpec,
    build_provider,
    merge_providers,
)


def test_registry_comes_from_core_presets():
    assert DEFAULT_PROVIDER == "anthropic"
    assert {"anthropic", "openai", "deepseek", "google", "gigachat", "yandex", "ollama"} <= set(
        PROVIDERS
    )
    assert PROVIDERS["deepseek"].api == "openai_chat"
    assert PROVIDERS["deepseek"].env_var == "DEEPSEEK_API_KEY"


def test_build_provider_uses_env_key_and_default_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    provider, model = build_provider("anthropic", None)
    assert isinstance(provider, AnthropicMessages)
    assert model == PROVIDERS["anthropic"].default_model


def test_missing_key_message(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(LunaConfigError, match="luna setup"):
        build_provider("deepseek", None)


def test_stored_key_is_used(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        "luna.config.credentials.get_api_key",
        lambda provider: "stored" if provider == "deepseek" else None,
    )
    provider, _ = build_provider("deepseek", "deepseek-chat")
    assert provider.api_key == "stored"


def test_custom_provider_is_openai_compatible_and_cannot_shadow_builtins():
    custom = {
        "local": ProviderSpec("local", "openai_chat", "llama3", None, "http://localhost:8080/v1"),
        "anthropic": ProviderSpec("anthropic", "openai_chat", "x", None, "http://evil"),
    }
    registry = merge_providers(custom)
    provider, model = build_provider("local", None, registry=registry)
    assert isinstance(provider, OpenAIChat)
    assert provider.base_url == "http://localhost:8080/v1" and model == "llama3"
    assert registry["anthropic"].api == "anthropic"


def test_unknown_provider():
    with pytest.raises(LunaConfigError, match="Unknown provider"):
        build_provider("grok", None)
