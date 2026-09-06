"""Provider registry: map a provider key to a LangChain chat model."""

from __future__ import annotations

import os
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel


class LunaConfigError(Exception):
    """Raised for unrecoverable configuration problems (bad provider, missing key)."""


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of a supported model provider."""

    key: str
    init_prefix: str
    default_model: str
    env_var: str | None
    pip_extra: str


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
}

DEFAULT_PROVIDER = "anthropic"


def _spec(provider: str) -> ProviderSpec:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(PROVIDERS)}."
        ) from None


def resolve_model_string(provider: str, model: str | None) -> str:
    """Return the ``<prefix>:<model>`` string passed to ``init_chat_model``."""
    spec = _spec(provider)
    return f"{spec.init_prefix}:{model or spec.default_model}"


def build_model(
    provider: str,
    model: str | None = None,
    model_kwargs: dict | None = None,
) -> BaseChatModel:
    """Instantiate a LangChain chat model for ``provider``.

    Raises:
        LunaConfigError: unknown provider, missing API key, or missing
            integration package.

    """
    spec = _spec(provider)
    if spec.env_var and not os.environ.get(spec.env_var):
        raise LunaConfigError(
            f"{spec.env_var} is not set. Export it or add it to your .env "
            f"(see .env.example), or pick another provider with --provider."
        )
    from langchain.chat_models import init_chat_model

    try:
        return init_chat_model(resolve_model_string(provider, model), **(model_kwargs or {}))
    except ImportError as exc:
        raise LunaConfigError(
            f"The {spec.key} integration is not installed. "
            f'Run:  pip install "luna-simple[{spec.pip_extra}]"'
        ) from exc
