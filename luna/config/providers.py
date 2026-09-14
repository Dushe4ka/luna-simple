"""Provider registry: map a provider key to a LangChain chat model."""

from __future__ import annotations

import os
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel


class LunaConfigError(Exception):
    """Raised for unrecoverable configuration problems (bad provider, missing key)."""


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of a supported model provider.

    ``base_url`` is only set for providers reached through the generic
    OpenAI-compatible path (Cerebras, and any user-defined
    ``[provider.custom.<name>]`` entry): for those, ``init_prefix`` is
    always ``"openai"`` and ``build_model`` injects ``base_url`` into the
    keyword arguments passed to ``init_chat_model`` instead of relying on
    a dedicated per-provider integration package.
    """

    key: str
    init_prefix: str
    default_model: str
    env_var: str | None
    pip_extra: str
    base_url: str | None = None


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        "anthropic", "anthropic", "claude-sonnet-4-5", "ANTHROPIC_API_KEY", "anthropic"
    ),
    "deepseek": ProviderSpec(
        "deepseek", "deepseek", "deepseek-chat", "DEEPSEEK_API_KEY", "deepseek"
    ),
    "openai": ProviderSpec("openai", "openai", "gpt-4.1", "OPENAI_API_KEY", "openai"),
    "google": ProviderSpec("google", "google_genai", "gemini-2.5-pro", "GOOGLE_API_KEY", "google"),
    "ollama": ProviderSpec("ollama", "ollama", "qwen2.5-coder", None, "ollama"),
    "mistral": ProviderSpec(
        "mistral", "mistralai", "mistral-large-latest", "MISTRAL_API_KEY", "mistral"
    ),
    "xai": ProviderSpec("xai", "xai", "grok-4", "XAI_API_KEY", "xai"),
    "groq": ProviderSpec("groq", "groq", "openai/gpt-oss-120b", "GROQ_API_KEY", "groq"),
    "fireworks": ProviderSpec(
        "fireworks",
        "fireworks",
        "accounts/fireworks/models/qwen3p5-397b-a17b",
        "FIREWORKS_API_KEY",
        "fireworks",
    ),
    "together": ProviderSpec(
        "together",
        "together",
        "Qwen/Qwen2.5-Coder-32B-Instruct",
        "TOGETHER_API_KEY",
        "together",
    ),
    "openrouter": ProviderSpec(
        "openrouter",
        "openrouter",
        "anthropic/claude-sonnet-4-6",
        "OPENROUTER_API_KEY",
        "openrouter",
    ),
    "perplexity": ProviderSpec("perplexity", "perplexity", "sonar", "PPLX_API_KEY", "perplexity"),
    "cerebras": ProviderSpec(
        "cerebras",
        "openai",
        "gpt-oss-120b",
        "CEREBRAS_API_KEY",
        "openai",
        "https://api.cerebras.ai/v1",
    ),
}

DEFAULT_PROVIDER = "anthropic"


def merge_providers(custom: dict[str, ProviderSpec]) -> dict[str, ProviderSpec]:
    """Combine user-defined custom providers with the built-in registry.

    A custom entry can never shadow a built-in provider name — if a user's
    ``[provider.custom.<name>]`` collides with a built-in key, the built-in
    ``ProviderSpec`` wins silently (no error): this keeps well-known names
    like ``anthropic``/``openai`` from ever being redirected to an
    unexpected endpoint by a stray config entry.
    """
    merged = dict(custom)
    merged.update(PROVIDERS)
    return merged


def _spec(provider: str, registry: dict[str, ProviderSpec] | None = None) -> ProviderSpec:
    reg = PROVIDERS if registry is None else registry
    try:
        return reg[provider]
    except KeyError:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(sorted(reg))}."
        ) from None


def resolve_model_string(
    provider: str,
    model: str | None,
    registry: dict[str, ProviderSpec] | None = None,
) -> str:
    """Return the ``<prefix>:<model>`` string passed to ``init_chat_model``."""
    spec = _spec(provider, registry)
    return f"{spec.init_prefix}:{model or spec.default_model}"


def build_model(
    provider: str,
    model: str | None = None,
    model_kwargs: dict | None = None,
    *,
    registry: dict[str, ProviderSpec] | None = None,
) -> BaseChatModel:
    """Instantiate a LangChain chat model for ``provider``.

    Raises:
        LunaConfigError: unknown provider, missing API key, or missing
            integration package.

    """
    spec = _spec(provider, registry)
    if spec.env_var and not os.environ.get(spec.env_var):
        from luna.config.credentials import apply_stored_key

        apply_stored_key(provider, spec.env_var)
    if spec.env_var and not os.environ.get(spec.env_var):
        raise LunaConfigError(
            f"No API key for {spec.key}. Run 'luna setup' to configure one, "
            f"export {spec.env_var}, or pick another provider with --provider."
        )
    from langchain.chat_models import init_chat_model

    kwargs = dict(model_kwargs or {})
    if spec.base_url:
        kwargs.setdefault("base_url", spec.base_url)
        if spec.env_var:
            kwargs.setdefault("api_key", os.environ[spec.env_var])
    try:
        return init_chat_model(resolve_model_string(provider, model, registry), **kwargs)
    except ImportError as exc:
        raise LunaConfigError(
            f"The {spec.key} integration is not installed. "
            f'Run:  pip install "luna-simple[{spec.pip_extra}]"'
        ) from exc
