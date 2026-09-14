import pytest

from luna.config.providers import (
    PROVIDERS,
    LunaConfigError,
    ProviderSpec,
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


def test_provider_spec_base_url_defaults_to_none():
    assert PROVIDERS["anthropic"].base_url is None


def test_spec_accepts_an_explicit_registry():
    custom = {
        "fake": ProviderSpec(
            "fake", "openai", "fake-model", "FAKE_API_KEY", "openai", "https://fake.example/v1"
        )
    }
    from luna.config.providers import _spec

    assert _spec("fake", custom).key == "fake"
    with pytest.raises(LunaConfigError):
        _spec("fake")  # not in the default PROVIDERS registry


def test_resolve_model_string_uses_given_registry():
    custom = {
        "fake": ProviderSpec(
            "fake", "openai", "fake-model", None, "openai", "https://fake.example/v1"
        )
    }
    assert resolve_model_string("fake", None, custom) == "openai:fake-model"


def test_build_model_injects_base_url_into_kwargs(monkeypatch):
    captured = {}

    def fake_init_chat_model(model_string, **kwargs):
        captured["model_string"] = model_string
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)
    monkeypatch.setenv("FAKE_API_KEY", "sk-test")
    custom = {
        "fake": ProviderSpec(
            "fake", "openai", "fake-model", "FAKE_API_KEY", "openai", "https://fake.example/v1"
        )
    }
    build_model("fake", registry=custom)
    assert captured["model_string"] == "openai:fake-model"
    assert captured["kwargs"]["base_url"] == "https://fake.example/v1"


def test_build_model_does_not_override_an_explicit_base_url_kwarg(monkeypatch):
    captured = {}

    def fake_init_chat_model(model_string, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)
    monkeypatch.setenv("FAKE_API_KEY", "sk-test")
    custom = {
        "fake": ProviderSpec(
            "fake", "openai", "fake-model", "FAKE_API_KEY", "openai", "https://fake.example/v1"
        )
    }
    build_model("fake", model_kwargs={"base_url": "https://override.example/v1"}, registry=custom)
    assert captured["kwargs"]["base_url"] == "https://override.example/v1"
