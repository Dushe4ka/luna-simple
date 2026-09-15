"""Interactive ``luna setup`` wizard: pick a provider, model, and API key.

Only lists built-in providers (``luna.config.providers.PROVIDERS``) — a
user-defined ``[provider.custom.<name>]`` (see ``config.py``) is configured
by hand in ``.luna.toml``/``config.toml``, not offered here, since the
wizard has no way to prompt for an arbitrary provider's base_url/env_var
pair sensibly.
"""

from __future__ import annotations

import getpass
import os
from collections.abc import Callable, Mapping

from rich.console import Console

from luna.config import model_discovery
from luna.config.config import set_config_values
from luna.config.credentials import get_api_key, mask_key, set_api_key
from luna.config.providers import DEFAULT_PROVIDER, PROVIDERS, ProviderSpec
from luna.ui.interact import arrow_confirm, arrow_pick
from luna.ui.theme import PALETTE


def _choose_provider(console: Console, input_fn: Callable[[str], str]) -> str:
    keys = list(PROVIDERS)
    default_idx = keys.index(DEFAULT_PROVIDER) + 1
    options: list[tuple[str, str]] = []
    for key in keys:
        spec = PROVIDERS[key]
        note = "local, no key" if spec.env_var is None else spec.env_var
        options.append((key, f"{key}  ({note})"))

    picked = arrow_pick(console, input_fn, options, default=DEFAULT_PROVIDER)
    if picked is not None:
        return picked

    console.print(f"[{PALETTE['peri']}]Choose a provider:[/]")
    for i, key in enumerate(keys, 1):
        spec = PROVIDERS[key]
        note = "local, no key" if spec.env_var is None else spec.env_var
        console.print(f"  {i}. {key}  [dim {PALETTE['blue']}]({note})[/]")

    while True:
        raw = input_fn(f"provider [{default_idx}]: ").strip().lower()
        if not raw:
            return DEFAULT_PROVIDER
        if raw in PROVIDERS:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        console.print(f"[{PALETTE['mauve']}]pick 1-{len(keys)} or a provider name[/]")


def choose_model(
    console: Console,
    input_fn: Callable[[str], str],
    provider: str,
    spec: ProviderSpec,
    *,
    api_key: str | None,
) -> str:
    """Pick a model id: live list, then known_models.toml, then free text.

    A blank free-text answer always returns ``spec.default_model`` —
    callers must write that return value unconditionally, overwriting
    any previously-stored ``model.name`` (this is the fix for the bug
    where a stale/garbage model name could never be reset via the
    wizard).
    """
    models = model_discovery.list_models(provider, spec, api_key=api_key)
    if models is None:
        models = model_discovery.known_models(provider)

    if not models:
        model = input_fn(f"model [{spec.default_model}]: ").strip()
        return model or spec.default_model

    default = spec.default_model if spec.default_model in models else models[0]
    manual_entry = "\x00__manual_entry__"
    options = [(m, m) for m in models] + [(manual_entry, "… type a model id manually")]
    picked = arrow_pick(console, input_fn, options, default=default)
    if picked == manual_entry:
        model = input_fn(f"model [{spec.default_model}]: ").strip()
        return model or spec.default_model
    if picked is not None:
        return picked

    console.print(f"[{PALETTE['peri']}]Choose a model:[/]")
    for i, m in enumerate(models, 1):
        console.print(f"  {i}. {m}")
    default_idx = models.index(default) + 1
    while True:
        raw = input_fn(f"model [{default_idx}]: ").strip()
        if not raw:
            return default
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(models):
                return models[idx - 1]
            console.print(f"[{PALETTE['mauve']}]pick 1-{len(models)} or a model name[/]")
            continue
        return raw


def run_setup(
    console: Console,
    *,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run the wizard. Returns a process exit code."""
    console.print(f"[bold {PALETTE['accent']}]Luna setup[/]\n")

    provider = _choose_provider(console, input_fn)
    spec = PROVIDERS[provider]

    resolved_key: str | None = None
    creds_file = None
    if spec.env_var is None:
        console.print(
            f"\n[{PALETTE['blue']}]{provider} runs locally - no API key needed."
            f" Set OLLAMA_HOST if it is not on the default port.[/]"
        )
    else:
        env_map = env if env is not None else os.environ
        existing = env_map.get(spec.env_var) or get_api_key(provider, env=env)
        resolved_key = existing
        prompt = "replace stored key" if existing else "paste your API key"
        if existing:
            console.print(f"\n[{PALETTE['blue']}]a key is already stored ({mask_key(existing)})[/]")
            picked = arrow_confirm(console, input_fn, "replace it?", default=False)
            if picked is None:
                picked = input_fn("replace it? [y/N]: ").strip().lower() in ("y", "yes")
            if not picked:
                prompt = None
        if prompt is not None:
            key = getpass_fn(f"{spec.env_var} ({prompt}): ").strip()
            if key:
                creds_file = set_api_key(provider, key, env=env)
                resolved_key = key
            else:
                console.print(
                    f"[{PALETTE['mauve']}]no key entered - set one later with"
                    f" 'luna config set-key {provider}'[/]"
                )

    model = choose_model(console, input_fn, provider, spec, api_key=resolved_key)

    config_file = set_config_values({"model.provider": provider, "model.name": model}, env=env)

    console.print("\n[green]saved.[/]")
    console.print(f"  config:      {config_file}")
    if creds_file:
        console.print(f"  credentials: {creds_file}  [dim](mode 0600)[/]")
    console.print(
        f"\n[{PALETTE['peri']}]Run [bold]luna[/bold] to start, or"
        f' [bold]luna "your task"[/bold] for one-shot.[/]'
    )
    return 0
