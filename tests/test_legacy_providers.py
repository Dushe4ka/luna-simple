"""TEMPORARY: the LangChain model builder used by the legacy REPL/server agent.

Deleted together with ``luna/core/_legacy_agent.py`` (Tasks 29-32).
"""

import pytest

from luna.config.config import load_config
from luna.config.providers import PROVIDERS, LunaConfigError, ProviderSpec
from luna.core._legacy_agent import _model_kwargs, build_model_legacy


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    def fake_init_chat_model(model_string, **kwargs):
        seen["model_string"] = model_string
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)
    return seen


def _custom(env_var="FAKE_API_KEY", url="https://fake.example/v1"):
    return {"fake": ProviderSpec("fake", "openai_chat", "fake-model", env_var, url)}


def test_missing_api_key_raises_with_hint(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LunaConfigError) as exc:
        build_model_legacy("anthropic")
    assert "ANTHROPIC_API_KEY" in str(exc.value) and "luna setup" in str(exc.value)


def test_unknown_provider_raises():
    with pytest.raises(LunaConfigError, match="Unknown provider"):
        build_model_legacy("grok")


def test_ollama_needs_no_key(monkeypatch, captured):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    build_model_legacy("ollama")
    assert captured["model_string"] == f"ollama:{PROVIDERS['ollama'].default_model}"
    assert "api_key" not in captured["kwargs"] and "base_url" not in captured["kwargs"]


def test_missing_integration_names_the_pip_extra(monkeypatch):
    def boom(model_string, **kwargs):
        raise ImportError("no module")

    monkeypatch.setattr("langchain.chat_models.init_chat_model", boom)
    with pytest.raises(LunaConfigError, match=r"luna-simple\[ollama\]"):
        build_model_legacy("ollama")


@pytest.mark.parametrize(
    ("provider", "prefix"),
    [
        ("anthropic", "anthropic"),
        ("deepseek", "deepseek"),
        ("openai", "openai"),
        ("google", "google_genai"),
        ("mistral", "mistralai"),
        ("xai", "xai"),
        ("groq", "groq"),
        ("fireworks", "fireworks"),
        ("together", "together"),
        ("openrouter", "openrouter"),
        ("perplexity", "perplexity"),
    ],
)
def test_native_langchain_prefix(monkeypatch, captured, provider, prefix):
    monkeypatch.setenv(PROVIDERS[provider].env_var, "k")
    build_model_legacy(provider, "m1")
    assert captured["model_string"] == f"{prefix}:m1"
    assert "base_url" not in captured["kwargs"]  # native integrations know their endpoint


def test_cerebras_uses_the_openai_base_url_path(monkeypatch, captured):
    monkeypatch.setenv("CEREBRAS_API_KEY", "sk-c")
    build_model_legacy("cerebras")
    assert captured["model_string"] == "openai:gpt-oss-120b"
    assert captured["kwargs"]["base_url"] == "https://api.cerebras.ai/v1"
    assert captured["kwargs"]["api_key"] == "sk-c"


def test_build_model_injects_base_url_into_kwargs(monkeypatch, captured):
    monkeypatch.setenv("FAKE_API_KEY", "sk-test")
    build_model_legacy("fake", registry=_custom())
    assert captured["model_string"] == "openai:fake-model"
    assert captured["kwargs"]["base_url"] == "https://fake.example/v1"


def test_build_model_does_not_override_an_explicit_base_url_kwarg(monkeypatch, captured):
    monkeypatch.setenv("FAKE_API_KEY", "sk-test")
    build_model_legacy(
        "fake", model_kwargs={"base_url": "https://override.example/v1"}, registry=_custom()
    )
    assert captured["kwargs"]["base_url"] == "https://override.example/v1"


def test_build_model_injects_the_providers_own_api_key(monkeypatch, captured):
    monkeypatch.setenv("FAKE_API_KEY", "sk-fake")
    # a stray OPENAI_API_KEY must never reach the custom endpoint
    monkeypatch.setenv("OPENAI_API_KEY", "sk-wrong-openai")
    build_model_legacy("fake", registry=_custom())
    assert captured["kwargs"]["api_key"] == "sk-fake"


def test_build_model_does_not_override_an_explicit_api_key_kwarg(monkeypatch, captured):
    monkeypatch.setenv("FAKE_API_KEY", "sk-fake")
    build_model_legacy("fake", model_kwargs={"api_key": "sk-explicit"}, registry=_custom())
    assert captured["kwargs"]["api_key"] == "sk-explicit"


def test_build_model_sends_no_api_key_for_a_keyless_base_url_provider(captured):
    build_model_legacy("fake", registry=_custom(env_var=None, url="http://localhost:8000/v1"))
    assert captured["kwargs"]["base_url"] == "http://localhost:8000/v1"
    assert "api_key" not in captured["kwargs"]


@pytest.mark.parametrize("provider", ["gigachat", "yandex", "openai_chat"])
def test_core_only_presets_are_refused_on_the_legacy_path(monkeypatch, captured, provider):
    monkeypatch.setenv(PROVIDERS[provider].env_var, "k")
    with pytest.raises(LunaConfigError, match="needs the new core"):
        build_model_legacy(provider)
    assert captured == {}


def test_model_kwargs_default_max_retries_to_zero(tmp_path):
    """A provider SDK retry restarts a streamed generation and duplicates shown text."""
    cfg = load_config({"temperature": 0.2, "max_tokens": 1000}, env={}, cwd=str(tmp_path))
    assert _model_kwargs(cfg) == {"max_retries": 0, "temperature": 0.2, "max_tokens": 1000}
    assert _model_kwargs(load_config({}, env={}, cwd=str(tmp_path))) == {"max_retries": 0}
