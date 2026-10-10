import pytest
from luna_core.errors import LunaCoreError
from luna_core.providers import PRESETS, Preset, resolve
from luna_core.providers.anthropic import AnthropicMessages
from luna_core.providers.gigachat import GigaChat
from luna_core.providers.google import GoogleGenAI
from luna_core.providers.openai_chat import OpenAIChat
from luna_core.providers.openai_responses import OpenAIResponses


def test_every_preset_resolves_with_its_key():
    for key, preset in PRESETS.items():
        env = {preset.env_var: "x"} if preset.env_var else {}
        if key == "yandex":
            env["YANDEX_FOLDER_ID"] = "b1g"
        provider, model = resolve(key, env=env)
        assert provider.name == key
        assert model


@pytest.mark.parametrize(
    ("key", "cls"),
    [
        ("anthropic", AnthropicMessages),
        ("openai", OpenAIResponses),
        ("deepseek", OpenAIChat),
        ("google", GoogleGenAI),
        ("gigachat", GigaChat),
    ],
)
def test_preset_api_classes(key, cls):
    provider, _ = resolve(key, env={PRESETS[key].env_var: "x"})
    assert isinstance(provider, cls)


def test_missing_key_names_the_env_var():
    with pytest.raises(LunaCoreError, match="DEEPSEEK_API_KEY"):
        resolve("deepseek", env={})


def test_explicit_key_and_model_win():
    provider, model = resolve("deepseek", model="deepseek-reasoner", api_key="k", env={})
    assert model == "deepseek-reasoner" and provider.api_key == "k"
    assert provider.send_reasoning is True and provider.base_url == "https://api.deepseek.com"


def test_unknown_provider_lists_choices():
    with pytest.raises(LunaCoreError, match="anthropic"):
        resolve("nope", env={})


def test_yandex_model_uri_and_folder_header():
    provider, model = resolve("yandex", env={"YANDEX_API_KEY": "k", "YANDEX_FOLDER_ID": "b1g"})
    assert model == "gpt://b1g/yandexgpt/latest"
    assert provider.headers["OpenAI-Project"] == "b1g"
    with pytest.raises(LunaCoreError, match="YANDEX_FOLDER_ID"):
        resolve("yandex", env={"YANDEX_API_KEY": "k"})


def test_ollama_needs_no_key():
    provider, _ = resolve("ollama", env={})
    assert provider.api_key is None


def test_custom_provider_cannot_shadow_builtin():
    custom = {
        "anthropic": Preset("anthropic", "openai_chat", "m", None, "https://evil.example/v1"),
        "local": Preset("local", "openai_chat", "llama", None, "http://localhost:8080/v1"),
    }
    provider, _ = resolve("anthropic", custom=custom, env={"ANTHROPIC_API_KEY": "x"})
    assert isinstance(provider, AnthropicMessages)
    provider, model = resolve("local", custom=custom, env={})
    assert provider.base_url == "http://localhost:8080/v1" and model == "llama"


def test_gigachat_options_from_env():
    provider, _ = resolve(
        "gigachat",
        env={"GIGACHAT_CREDENTIALS": "c", "GIGACHAT_SCOPE": "GIGACHAT_API_CORP"},
    )
    assert provider.scope == "GIGACHAT_API_CORP"
