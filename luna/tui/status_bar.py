"""Bottom status bar: model, provider, context/cost usage, undo depth."""

from __future__ import annotations

from textual.reactive import reactive
from textual.widget import Widget


def format_status_line(
    *,
    model: str,
    cost_usd: float,
    context_file: str | None,
    plan_mode: bool,
    undo_depth: int,
    provider: str = "",
    usage_summary: str = "",
) -> str:
    """Render the one-line status bar text.

    ``usage_summary`` (typically ``luna.config.usage.indicator_line``'s
    output — the same "ctx ~X/Y · turn ... · session ... · $Z" line the
    old REPL printed after every turn) replaces the plain ``cost_usd``
    figure once at least one turn has been recorded; before that, a bare
    "$0.00" is more misleading than informative for a session that hasn't
    sent anything yet.
    """
    parts = [f"{model} ({provider})" if model and provider else model] if model else []
    parts.append(usage_summary if usage_summary else f"${cost_usd:.2f}")
    if context_file:
        parts.append(f"@{context_file}")
    parts.append(f"plan: {'on' if plan_mode else 'off'}")
    parts.append(f"undo: {undo_depth}")
    return " · ".join(parts)


class StatusBar(Widget):
    """Live-updating one-line status bar."""

    model: reactive[str] = reactive("")
    provider: reactive[str] = reactive("")
    cost_usd: reactive[float] = reactive(0.0)
    usage_summary: reactive[str] = reactive("")
    context_file: reactive[str | None] = reactive(None)
    plan_mode: reactive[bool] = reactive(False)
    undo_depth: reactive[int] = reactive(0)

    def render(self) -> str:
        """Return the current one-line status text."""
        return format_status_line(
            model=self.model,
            provider=self.provider,
            cost_usd=self.cost_usd,
            usage_summary=self.usage_summary,
            context_file=self.context_file,
            plan_mode=self.plan_mode,
            undo_depth=self.undo_depth,
        )
