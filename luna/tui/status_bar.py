"""Bottom status bar: model, cost, @file context, /plan state, undo depth."""

from __future__ import annotations

from textual.reactive import reactive
from textual.widget import Widget


def format_status_line(
    *, model: str, cost_usd: float, context_file: str | None, plan_mode: bool, undo_depth: int
) -> str:
    """Render the one-line status bar text."""
    parts = [model, f"${cost_usd:.2f}"]
    if context_file:
        parts.append(f"@{context_file}")
    parts.append(f"plan: {'on' if plan_mode else 'off'}")
    parts.append(f"undo: {undo_depth}")
    return " · ".join(parts)


class StatusBar(Widget):
    """Live-updating one-line status bar."""

    model: reactive[str] = reactive("")
    cost_usd: reactive[float] = reactive(0.0)
    context_file: reactive[str | None] = reactive(None)
    plan_mode: reactive[bool] = reactive(False)
    undo_depth: reactive[int] = reactive(0)

    def render(self) -> str:
        """Return the current one-line status text."""
        return format_status_line(
            model=self.model,
            cost_usd=self.cost_usd,
            context_file=self.context_file,
            plan_mode=self.plan_mode,
            undo_depth=self.undo_depth,
        )
