"""Model catalogue: context windows, prices and capabilities.

Keys are substrings of model ids; the longest key contained in the model id
wins, so ``claude-sonnet-4-5-20250929`` matches ``claude-sonnet-4``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from importlib import resources

DEFAULT_WINDOW = 128_000


@dataclass(frozen=True)
class ModelInfo:
    """What the core knows about a model. Prices are USD per million tokens."""

    key: str
    context_window: int
    max_output: int | None
    input_price: float | None
    output_price: float | None
    cache_read_price: float | None
    cache_write_price: float | None
    vision: bool
    reasoning: bool


@cache
def _packaged() -> dict[str, dict]:
    text = resources.files("luna_core").joinpath("data/models.toml").read_text("utf-8")
    return tomllib.loads(text).get("models", {})


def _info(key: str, entry: dict) -> ModelInfo:
    def num(name: str) -> float | None:
        value = entry.get(name)
        return float(value) if value is not None else None

    return ModelInfo(
        key=key,
        context_window=int(entry.get("window", DEFAULT_WINDOW)),
        max_output=int(entry["max_output"]) if "max_output" in entry else None,
        input_price=num("input"),
        output_price=num("output"),
        cache_read_price=num("cache_read"),
        cache_write_price=num("cache_write"),
        vision=bool(entry.get("vision", False)),
        reasoning=bool(entry.get("reasoning", False)),
    )


def _best(entries: dict[str, dict], model: str) -> tuple[str, dict] | None:
    needle = model.lower()
    hits = [(k, v) for k, v in entries.items() if k.lower() in needle]
    return max(hits, key=lambda kv: len(kv[0])) if hits else None


def lookup(model: str, overrides: dict | None = None) -> ModelInfo:
    """Return the best catalogue entry for ``model`` (overrides first)."""
    for source in (overrides or {}, _packaged()):
        hit = _best(source, model or "")
        if hit is not None:
            return _info(*hit)
    return ModelInfo(model or "", DEFAULT_WINDOW, None, None, None, None, None, False, False)
