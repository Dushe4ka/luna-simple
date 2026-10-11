"""Model roles: which model handles which kind of work."""

from __future__ import annotations

from dataclasses import dataclass

from luna_core import catalog
from luna_core.providers.base import Provider

RoleTarget = str | tuple[Provider, str]
ROLES = ("fast", "plan", "vision", "summarize")


@dataclass
class ModelRoles:
    """Optional per-role models; unset roles fall back to the default model."""

    fast: RoleTarget | None = None
    plan: RoleTarget | None = None
    vision: RoleTarget | None = None
    summarize: RoleTarget | None = None

    def get(self, role: str, provider: Provider, model: str) -> tuple[Provider, str]:
        """Return the provider and model for ``role``."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        target = getattr(self, role)
        if target is None:
            return provider, model
        if isinstance(target, tuple):
            return target
        return provider, target

    def for_turn(
        self,
        provider: Provider,
        model: str,
        *,
        images: bool,
        plan: bool,
        overrides: dict | None = None,
    ) -> tuple[Provider, str, str | None]:
        """Pick the model for a turn: vision for images the default can't see, plan in plan mode."""
        if images and self.vision is not None and not catalog.lookup(model, overrides).vision:
            return (*self.get("vision", provider, model), "vision")
        if plan and self.plan is not None:
            return (*self.get("plan", provider, model), "plan")
        return provider, model, None


__all__ = ["ModelRoles", "ROLES", "RoleTarget"]
