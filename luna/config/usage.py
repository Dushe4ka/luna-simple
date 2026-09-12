"""Token accounting for a Luna session (best-effort, never raises)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources

__all__ = [
    "TurnUsage",
    "SessionUsage",
    "context_window",
    "price",
    "indicator_line",
]

_WINDOWS: dict[str, int] = {
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "claude-haiku": 200_000,
    "claude-3": 200_000,
    "gpt-4.1": 1_000_000,
    "gpt-4o": 128_000,
    "gpt-5": 400_000,
    "o1": 200_000,
    "o3": 200_000,
    "gemini-2.5": 1_000_000,
    "gemini-1.5": 1_000_000,
    "deepseek": 64_000,
    "qwen2.5-coder": 32_000,
}
_FALLBACK = 200_000

_REGISTRY_CACHE: dict[str, dict] | None = None


def _load_registry() -> dict[str, dict]:
    """Read the packaged model registry once, caching the result."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE
    try:
        text = resources.files("luna.config").joinpath("models.toml").read_text()
        _REGISTRY_CACHE = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError, ModuleNotFoundError):
        _REGISTRY_CACHE = {}
    return _REGISTRY_CACHE


def _match(entries: dict, needle: str) -> dict | None:
    for key, entry in entries.items():
        if key.lower() in needle:
            return entry
    return None


def _int(value: object) -> int:
    """Coerce loosely-typed metadata to a non-negative int, never raising."""
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


@dataclass
class TurnUsage:
    """Token counts accumulated across the chunks of a single turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def merge(self, meta: dict | None) -> None:
        """Fold one ``usage_metadata`` mapping in. ``None`` is a no-op.

        ``output_tokens`` and ``total_tokens`` accumulate (summed for cost),
        but ``input_tokens`` tracks the *last* non-zero value seen: it is the
        current prompt size, not a running sum.
        """
        if not isinstance(meta, dict):
            return
        in_tok = _int(meta.get("input_tokens"))
        out_tok = _int(meta.get("output_tokens"))
        total = _int(meta.get("total_tokens")) or (in_tok + out_tok)
        if in_tok:
            self.input_tokens = in_tok
        self.output_tokens += out_tok
        self.total_tokens += total

    @property
    def has_tokens(self) -> bool:
        """True when any token count was recorded for this turn."""
        return bool(self.input_tokens or self.output_tokens or self.total_tokens)


@dataclass
class SessionUsage:
    """All recorded turns for one REPL session."""

    turns: list[TurnUsage] = field(default_factory=list)

    def add_turn(self, turn: TurnUsage | None) -> None:
        """Append ``turn`` if it carried any tokens; ignore empty turns."""
        if isinstance(turn, TurnUsage) and turn.has_tokens:
            self.turns.append(turn)

    @property
    def totals(self) -> tuple[int, int, int]:
        """``(input, output, total)`` summed across every recorded turn."""
        return (
            sum(t.input_tokens for t in self.turns),
            sum(t.output_tokens for t in self.turns),
            sum(t.total_tokens for t in self.turns),
        )

    @property
    def last_prompt_tokens(self) -> int:
        """Input tokens of the most recent turn (0 when none recorded)."""
        return self.turns[-1].input_tokens if self.turns else 0

    def cost(self, provider: str, model: str | None, overrides: dict | None = None) -> float | None:
        """Total USD for every recorded turn, or ``None`` when no price is known."""
        p = price(provider, model, overrides)
        if p is None:
            return None
        in_price, out_price = p
        in_tok, out_tok, _ = self.totals
        return in_tok / 1_000_000 * in_price + out_tok / 1_000_000 * out_price


def context_window(provider: str, model: str | None, overrides: dict | None = None) -> int:
    """Best-effort context-window size for ``model`` (fallback 200k)."""
    needle = (model or provider or "").lower()
    entry = _match(overrides, needle) if overrides else None
    if entry is None:
        entry = _match(_load_registry(), needle)
    if entry and "window" in entry:
        return int(entry["window"])
    for key, size in _WINDOWS.items():
        if key in needle:
            return size
    return _FALLBACK


def price(
    provider: str, model: str | None, overrides: dict | None = None
) -> tuple[float, float] | None:
    """``(input_usd_per_1m, output_usd_per_1m)`` for ``model``, or ``None``."""
    needle = (model or provider or "").lower()
    entry = _match(overrides, needle) if overrides else None
    if entry is None:
        entry = _match(_load_registry(), needle)
    if entry and "input" in entry and "output" in entry:
        return float(entry["input"]), float(entry["output"])
    return None


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def indicator_line(
    session: SessionUsage, provider: str, model: str | None, overrides: dict | None = None
) -> str:
    """One-line dim summary printed after each model turn."""
    win = context_window(provider, model, overrides)
    used = session.last_prompt_tokens
    _, _, total = session.totals
    last = session.turns[-1] if session.turns else TurnUsage()
    line = (
        f"ctx ~{_k(used)}/{_k(win)} · turn {_k(last.input_tokens)} in / "
        f"{_k(last.output_tokens)} out · session {_k(total)}"
    )
    cost = session.cost(provider, model, overrides)
    if cost is not None:
        line += f" · ${cost:.4f}"
    return line
