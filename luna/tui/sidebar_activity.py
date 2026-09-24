"""Right sidebar: live tool-call activity.

Ported from luna/ui/progress.py's visual language (a pending list plus
resolved entries).
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView


@dataclass
class _Pending:
    name: str
    detail: str


class ActivitySidebar(Widget):
    """Tracks in-flight tool calls for the current turn."""

    def __init__(self) -> None:
        super().__init__()
        self._pending: dict[str, _Pending] = {}

    def compose(self):
        """Yield the ListView that renders in-flight tool calls."""
        yield ListView(id="activity-list")

    def pending_count(self) -> int:
        """Return the number of tool calls currently tracked as in flight."""
        return len(self._pending)

    def tool_started(self, call_id: str, name: str, args: dict) -> None:
        """Register a newly-requested tool call."""
        self._pending[call_id] = _Pending(name=name, detail="")
        if self.is_mounted:
            self._render()

    def tool_finished(self, call_id: str, name: str, ok: bool, detail: str) -> None:
        """Mark a tool call as resolved and stop tracking it."""
        self._pending.pop(call_id, None)
        if self.is_mounted:
            self._render()

    def _render(self) -> None:
        list_view = self.query_one("#activity-list", ListView)
        list_view.clear()
        for pending in self._pending.values():
            list_view.append(ListItem(Label(f"⏺ {pending.name}")))
