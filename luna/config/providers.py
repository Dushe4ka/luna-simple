"""Provider registry for the app: luna-core presets plus user custom providers."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from luna_core.providers import PRESETS, Preset, resolve
from luna_core.providers.base import Provider


class LunaConfigError(Exception):
    """Raised for unrecoverable configuration problems (bad provider, missing key)."""


@dataclass(frozen=True)
class ProviderSpec:
    """A provider the app can use: wire API, default model, key variable, endpoint."""

    key: str
    api: str
    default_model: str
    env_var: str | None
    base_url: str | None = None
    options: dict = field(default_factory=dict)

    def preset(self) -> Preset:
        """Return the core preset for this spec."""
        return Preset(
            self.key, self.api, self.default_model, self.env_var, self.base_url, dict(self.options)
        )


# The app's historical order first (the setup wizard numbers providers in
# this order), then every other core preset.
_ORDER = ("anthropic", "deepseek", "openai", "google", "ollama")
PROVIDERS: dict[str, ProviderSpec] = {
    k: ProviderSpec(p.key, p.api, p.default_model, p.env_var, p.base_url, dict(p.options))
    for k, p in sorted(
        PRESETS.items(), key=lambda kv: _ORDER.index(kv[0]) if kv[0] in _ORDER else len(_ORDER)
    )
}
DEFAULT_PROVIDER = "anthropic"


def merge_providers(custom: dict[str, ProviderSpec]) -> dict[str, ProviderSpec]:
    """Merge custom providers with the built-ins; a custom entry never shadows a built-in."""
    merged = dict(custom)
    merged.update(PROVIDERS)
    return merged


def build_provider(
    provider: str, model: str | None, *, registry: dict[str, ProviderSpec] | None = None
) -> tuple[Provider, str]:
    """Return the core adapter and model id for ``provider`` (stored keys applied)."""
    reg = PROVIDERS if registry is None else registry
    spec = reg.get(provider)
    if spec is None:
        raise LunaConfigError(
            f"Unknown provider {provider!r}. Choose one of: {', '.join(sorted(reg))}."
        )
    if spec.env_var and not os.environ.get(spec.env_var):
        from luna.config.credentials import apply_stored_key

        apply_stored_key(provider, spec.env_var)
    if spec.env_var and not os.environ.get(spec.env_var):
        raise LunaConfigError(
            f"No API key for {spec.key}. Run 'luna setup' to configure one, "
            f"export {spec.env_var}, or pick another provider with --provider."
        )
    custom = {provider: spec.preset()} if provider not in PRESETS else None
    try:
        return resolve(provider, model=model, custom=custom)
    except Exception as exc:  # LunaCoreError from the core (e.g. YANDEX_FOLDER_ID)
        raise LunaConfigError(str(exc)) from exc
