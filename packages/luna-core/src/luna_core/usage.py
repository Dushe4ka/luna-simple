"""Token usage and cost."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from luna_core.catalog import ModelInfo


@dataclass
class Usage:
    """Tokens for one request, a turn or a whole session.

    ``input`` is the full prompt size including cached tokens; ``cache_read``
    and ``cache_write`` are the parts of it served from / written to the
    provider's prompt cache. ``reasoning`` is the part of ``output`` spent on
    hidden reasoning, when the provider reports it.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0

    def __add__(self, other: Usage) -> Usage:
        """Sum two usages field by field."""
        return Usage(
            self.input + other.input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_write + other.cache_write,
            self.reasoning + other.reasoning,
        )

    @property
    def total(self) -> int:
        """Input plus output tokens."""
        return self.input + self.output

    def to_dict(self) -> dict:
        """JSON form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Usage:
        """Rebuild from :meth:`to_dict` output (unknown keys ignored)."""
        return cls(**{k: int(data.get(k, 0) or 0) for k in cls.__dataclass_fields__})


def cost(usage: Usage, info: ModelInfo) -> float | None:
    """USD cost of ``usage`` for ``info``'s prices, or ``None`` without prices."""
    if info.input_price is None or info.output_price is None:
        return None
    cache_read_price = (
        info.cache_read_price if info.cache_read_price is not None else info.input_price
    )
    cache_write_price = (
        info.cache_write_price if info.cache_write_price is not None else info.input_price
    )
    fresh = max(0, usage.input - usage.cache_read - usage.cache_write)
    total = (
        fresh * info.input_price
        + usage.cache_read * cache_read_price
        + usage.cache_write * cache_write_price
        + usage.output * info.output_price
    )
    return total / 1_000_000
