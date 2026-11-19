"""Limits and knobs of a team run (``[team]`` in the app config)."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass

_POSITIVE_INTS = (
    "max_concurrent",
    "max_members",
    "member_steps",
    "lead_steps",
    "max_attempts",
    "stall_wakes",
)
_POSITIVE_NUMBERS = ("rpm", "wall_clock", "verify_timeout")


@dataclass(frozen=True)
class TeamSettings:
    """Limits of one team run; defaults follow the spec (2.5)."""

    max_concurrent: int = 4
    max_members: int = 8
    member_steps: int = 30
    lead_steps: int = 60
    rpm: float = 30.0
    budget: float | None = None
    wall_clock: float = 7200.0
    verify_command: str = ""
    verify_timeout: float = 600.0
    max_attempts: int = 3
    stall_wakes: int = 3
    allow_sleep: bool = False
    models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject values a run cannot work with."""
        for name in _POSITIVE_INTS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"team {name} must be an integer >= 1, got {value!r}")
        for name in _POSITIVE_NUMBERS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"team {name} must be a number > 0, got {value!r}")
        budget = self.budget
        if budget is not None and (
            isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget < 0
        ):
            raise ValueError(f"team budget must be a number >= 0, got {budget!r}")

    def to_dict(self) -> dict:
        """JSON-friendly form (stored with the run)."""
        data = dataclasses.asdict(self)
        data["models"] = list(self.models)
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> TeamSettings:
        """Inverse of :meth:`to_dict`; unknown keys are ignored."""
        known = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        if "models" in values:
            values["models"] = tuple(values["models"])
        return cls(**values)
