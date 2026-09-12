"""Interactive ``luna setup`` wizard: pick a provider, model, and API key."""

from __future__ import annotations

import getpass
from collections.abc import Callable, Mapping

from rich.console import Console

from luna.config.config import set_config_values
from luna.config.credentials import get_api_key, mask_key, set_api_key
from luna.config.providers import DEFAULT_PROVIDER, PROVIDERS
from luna.ui.theme import PALETTE


def _choose_provider(console: Console, input_fn: Callable[[str], str]) -> str:
    keys = list(PROVIDERS)
    default_idx = keys.index(DEFAULT_PROVIDER) + 1
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

    default_model = spec.default_model
    model = input_fn(f"model [{default_model}]: ").strip()

    updates = {"model.provider": provider}
    if model and model != default_model:
        updates["model.name"] = model
    config_file = set_config_values(updates, env=env)

    creds_file = None
    if spec.env_var is None:
        console.print(
            f"\n[{PALETTE['blue']}]{provider} runs locally - no API key needed."
            f" Set OLLAMA_HOST if it is not on the default port.[/]"
        )
    else:
        existing = get_api_key(provider, env=env)
        prompt = "replace stored key" if existing else "paste your API key"
        if existing:
            console.print(f"\n[{PALETTE['blue']}]a key is already stored ({mask_key(existing)})[/]")
            if input_fn("replace it? [y/N]: ").strip().lower() not in ("y", "yes"):
                prompt = None
        if prompt is not None:
            key = getpass_fn(f"{spec.env_var} ({prompt}): ").strip()
            if key:
                creds_file = set_api_key(provider, key, env=env)
            else:
                console.print(
                    f"[{PALETTE['mauve']}]no key entered - set one later with"
                    f" 'luna config set-key {provider}'[/]"
                )

    console.print("\n[green]saved.[/]")
    console.print(f"  config:      {config_file}")
    if creds_file:
        console.print(f"  credentials: {creds_file}  [dim](mode 0600)[/]")
    console.print(
        f"\n[{PALETTE['peri']}]Run [bold]luna[/bold] to start, or"
        f' [bold]luna "your task"[/bold] for one-shot.[/]'
    )
    return 0
