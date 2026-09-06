import pytest

from luna.providers import (
    PROVIDERS,
    LunaConfigError,
    build_model,
    resolve_model_string,
)


def test_registry_has_five_providers():
    assert set(PROVIDERS) == {"anthropic", "deepseek", "openai", "google", "ollama"}


def test_anthropic_is_reference_default():
    assert PROVIDERS["anthropic"].default_model == "claude-sonnet-4-5"
    assert PROVIDERS["google"].init_prefix == "google_genai"


def test_resolve_model_string_uses_default():
    assert resolve_model_string("anthropic", None) == "anthropic:claude-sonnet-4-5"
    assert resolve_model_string("openai", "gpt-4o") == "openai:gpt-4o"


def test_unknown_provider_raises():
    with pytest.raises(LunaConfigError):
        resolve_model_string("grok", None)


def test_missing_api_key_raises_with_hint(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LunaConfigError) as exc:
        build_model("anthropic")
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_ollama_needs_no_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    try:
        build_model("ollama")
    except LunaConfigError as e:
        assert "langchain-ollama" in str(e) or "luna-simple[ollama]" in str(e)
