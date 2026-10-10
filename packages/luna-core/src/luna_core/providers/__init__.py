"""Model providers: adapters for the wire APIs and the registry of presets."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from luna_core.errors import LunaCoreError
from luna_core.providers.base import ModelRequest, Provider, RetryPolicy, StreamAccumulator
from luna_core.providers.fake import FakeProvider

APIS = ("openai_chat", "openai_responses", "anthropic", "google", "gigachat")


@dataclass(frozen=True)
class Preset:
    """A named provider: which wire API, default model, key variable, endpoint."""

    key: str
    api: str
    default_model: str
    env_var: str | None
    base_url: str | None = None
    options: dict = field(default_factory=dict)


def _p(
    key: str, api: str, model: str, env: str | None, url: str | None = None, **options: object
) -> Preset:
    return Preset(key, api, model, env, url, dict(options))


PRESETS: dict[str, Preset] = {
    p.key: p
    for p in (
        _p("anthropic", "anthropic", "claude-sonnet-5", "ANTHROPIC_API_KEY"),
        _p("openai", "openai_responses", "gpt-6-astra", "OPENAI_API_KEY"),
        _p(
            "openai_chat",
            "openai_chat",
            "gpt-6.1-sol",
            "OPENAI_API_KEY",
            max_tokens_field="max_completion_tokens",
        ),
        _p(
            "deepseek",
            "openai_chat",
            "deepseek-flash",
            "DEEPSEEK_API_KEY",
            "https://api.deepseek.com",
            send_reasoning=True,
        ),
        _p("google", "google", "gemini-3.1-pro-preview", "GOOGLE_API_KEY"),
        _p("gigachat", "gigachat", "GigaChat-2-Max", "GIGACHAT_CREDENTIALS"),
        _p(
            "yandex",
            "openai_chat",
            "yandexgpt/latest",
            "YANDEX_API_KEY",
            "https://llm.api.cloud.yandex.net/v1",
        ),
        _p("ollama", "openai_chat", "qwen3-coder", None, "http://localhost:11434/v1"),
        _p(
            "mistral",
            "openai_chat",
            "mistral-large-latest",
            "MISTRAL_API_KEY",
            "https://api.mistral.ai/v1",
        ),
        _p("xai", "openai_chat", "grok-4.7", "XAI_API_KEY", "https://api.x.ai/v1"),
        _p(
            "groq",
            "openai_chat",
            "openai/gpt-oss-120b",
            "GROQ_API_KEY",
            "https://api.groq.com/openai/v1",
        ),
        _p(
            "fireworks",
            "openai_chat",
            "accounts/fireworks/models/qwen3p5-397b-a17b",
            "FIREWORKS_API_KEY",
            "https://api.fireworks.ai/inference/v1",
        ),
        _p(
            "together",
            "openai_chat",
            "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8",
            "TOGETHER_API_KEY",
            "https://api.together.xyz/v1",
        ),
        _p(
            "openrouter",
            "openai_chat",
            "anthropic/claude-sonnet-5",
            "OPENROUTER_API_KEY",
            "https://openrouter.ai/api/v1",
        ),
        _p("perplexity", "openai_chat", "sonar", "PPLX_API_KEY", "https://api.perplexity.ai"),
        _p(
            "cerebras",
            "openai_chat",
            "gpt-oss-120b",
            "CEREBRAS_API_KEY",
            "https://api.cerebras.ai/v1",
        ),
    )
}


def build(api: str, **kwargs: object) -> Provider:
    """Instantiate the adapter for ``api`` (imports lazily)."""
    if api == "openai_chat":
        from luna_core.providers.openai_chat import OpenAIChat

        return OpenAIChat(**kwargs)
    if api == "openai_responses":
        from luna_core.providers.openai_responses import OpenAIResponses

        return OpenAIResponses(**kwargs)
    if api == "anthropic":
        from luna_core.providers.anthropic import AnthropicMessages

        return AnthropicMessages(**kwargs)
    if api == "google":
        from luna_core.providers.google import GoogleGenAI

        return GoogleGenAI(**kwargs)
    if api == "gigachat":
        from luna_core.providers.gigachat import GigaChat

        return GigaChat(**kwargs)
    raise LunaCoreError(f"unknown provider API {api!r}; expected one of {', '.join(APIS)}")


def resolve(
    provider: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    options: dict | None = None,
    custom: dict[str, Preset] | None = None,
    env: Mapping[str, str] = os.environ,
) -> tuple[Provider, str]:
    """Build the adapter for a provider key and return it with the model id.

    Built-in presets always win over ``custom`` entries with the same key,
    so a stray config entry can never redirect ``anthropic`` elsewhere.
    """
    registry = {**(custom or {}), **PRESETS}
    preset = registry.get(provider)
    if preset is None:
        raise LunaCoreError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(sorted(registry))}."
        )
    key = api_key or (env.get(preset.env_var) if preset.env_var else None)
    if preset.env_var and not key:
        raise LunaCoreError(f"No API key for {provider}: set {preset.env_var} or pass api_key.")
    model_id = model or preset.default_model
    kwargs: dict = {"name": preset.key, **preset.options, **(options or {})}
    url = base_url or preset.base_url
    if url:
        kwargs["base_url"] = url
    if preset.api == "gigachat":
        kwargs["credentials"] = key
        if env.get("GIGACHAT_SCOPE"):
            kwargs.setdefault("scope", env["GIGACHAT_SCOPE"])
        if env.get("GIGACHAT_CA_BUNDLE"):
            kwargs.setdefault("ca_bundle", env["GIGACHAT_CA_BUNDLE"])
    else:
        kwargs["api_key"] = key
    if preset.key == "yandex":
        folder = env.get("YANDEX_FOLDER_ID")
        if not folder:
            raise LunaCoreError("Yandex AI Studio needs YANDEX_FOLDER_ID (the cloud folder id).")
        if not model_id.startswith("gpt://"):
            model_id = f"gpt://{folder}/{model_id}"
        kwargs["headers"] = {"OpenAI-Project": folder, **kwargs.get("headers", {})}
    return build(preset.api, **kwargs), model_id


__all__ = [
    "APIS",
    "PRESETS",
    "FakeProvider",
    "ModelRequest",
    "Preset",
    "Provider",
    "RetryPolicy",
    "StreamAccumulator",
    "build",
    "resolve",
]
