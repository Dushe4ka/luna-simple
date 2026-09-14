import pytest

from luna.config.providers import (
    PROVIDERS,
    LunaConfigError,
    ProviderSpec,
    build_model,
    resolve_model_string,
)


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


def test_build_model_injects_the_providers_own_api_key_into_kwargs(monkeypatch):
    captured = {}

    def fake_init_chat_model(model_string, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)
    monkeypatch.setenv("FAKE_API_KEY", "sk-fake")
    # A stray OPENAI_API_KEY must never be what reaches the custom endpoint:
    # the base_url path always resolves to the "openai:" prefix, so without an
    # explicit api_key kwarg ChatOpenAI would silently fall back to this one.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-wrong-openai")
    custom = {
        "fake": ProviderSpec(
            "fake", "openai", "fake-model", "FAKE_API_KEY", "openai", "https://fake.example/v1"
        )
    }
    build_model("fake", registry=custom)
    assert captured["kwargs"]["api_key"] == "sk-fake"


def test_build_model_does_not_override_an_explicit_api_key_kwarg(monkeypatch):
    captured = {}

    def fake_init_chat_model(model_string, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)
    monkeypatch.setenv("FAKE_API_KEY", "sk-fake")
    custom = {
        "fake": ProviderSpec(
            "fake", "openai", "fake-model", "FAKE_API_KEY", "openai", "https://fake.example/v1"
        )
    }
    build_model("fake", model_kwargs={"api_key": "sk-explicit"}, registry=custom)
    assert captured["kwargs"]["api_key"] == "sk-explicit"


def test_build_model_sends_no_api_key_for_a_keyless_base_url_provider(monkeypatch):
    captured = {}

    def fake_init_chat_model(model_string, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)
    custom = {
        "fake": ProviderSpec(
            "fake", "openai", "fake-model", None, "openai", "http://localhost:8000/v1"
        )
    }
    build_model("fake", registry=custom)
    assert captured["kwargs"]["base_url"] == "http://localhost:8000/v1"
    assert "api_key" not in captured["kwargs"]


def test_registry_has_thirteen_providers():
    assert set(PROVIDERS) == {
        "anthropic",
        "deepseek",
        "openai",
        "google",
        "ollama",
        "mistral",
        "xai",
        "groq",
        "fireworks",
        "together",
        "openrouter",
        "perplexity",
        "cerebras",
    }


def test_mistral_uses_mistralai_prefix():
    assert PROVIDERS["mistral"].init_prefix == "mistralai"
    assert resolve_model_string("mistral", None) == "mistralai:mistral-large-latest"


def test_xai_uses_xai_prefix():
    assert PROVIDERS["xai"].init_prefix == "xai"
    assert resolve_model_string("xai", None) == "xai:grok-4"


def test_groq_uses_groq_prefix():
    assert PROVIDERS["groq"].init_prefix == "groq"
    assert resolve_model_string("groq", None) == "groq:openai/gpt-oss-120b"


def test_fireworks_uses_fireworks_prefix():
    assert PROVIDERS["fireworks"].init_prefix == "fireworks"
    assert resolve_model_string("fireworks", None) == (
        "fireworks:accounts/fireworks/models/qwen3p5-397b-a17b"
    )


def test_together_uses_together_prefix():
    assert PROVIDERS["together"].init_prefix == "together"
    assert resolve_model_string("together", None) == "together:Qwen/Qwen2.5-Coder-32B-Instruct"


def test_openrouter_uses_openrouter_prefix():
    assert PROVIDERS["openrouter"].init_prefix == "openrouter"
    assert resolve_model_string("openrouter", None) == "openrouter:anthropic/claude-sonnet-4-6"


def test_perplexity_uses_perplexity_prefix():
    assert PROVIDERS["perplexity"].init_prefix == "perplexity"
    assert resolve_model_string("perplexity", None) == "perplexity:sonar"


def test_new_native_providers_have_no_base_url():
    for key in ("mistral", "xai", "groq", "fireworks", "together", "openrouter", "perplexity"):
        assert PROVIDERS[key].base_url is None


def test_new_native_providers_each_use_their_own_pip_extra():
    expected = {
        "mistral": "mistral",
        "xai": "xai",
        "groq": "groq",
        "fireworks": "fireworks",
        "together": "together",
        "openrouter": "openrouter",
        "perplexity": "perplexity",
    }
    for key, extra in expected.items():
        assert PROVIDERS[key].pip_extra == extra


def test_cerebras_uses_base_url_path():
    spec = PROVIDERS["cerebras"]
    assert spec.init_prefix == "openai"
    assert spec.base_url == "https://api.cerebras.ai/v1"
    assert spec.env_var == "CEREBRAS_API_KEY"
    assert spec.pip_extra == "openai"
    assert resolve_model_string("cerebras", None) == "openai:gpt-oss-120b"
