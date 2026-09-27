"""Right sidebar: live tool-call activity.

Ported from luna/ui/progress.py's visual language — a pending list with a
pulsing "still working" indicator per entry — via ``PulseGlyph``
(luna/tui/widgets.py), Textual's equivalent of that module's
``rich.live.Live``-driven breathing dot.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView

from luna.tui.widgets import PulseGlyph


@dataclass
class _Pending:
    name: str
    started_at: float = field(default_factory=time.monotonic)


class ActivitySidebar(Widget):
    """Tracks in-flight tool calls for the current turn.

    Styling lives in ``luna/tui/luna.tcss`` (external stylesheet), not a
    ``DEFAULT_CSS`` string here.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending: dict[str, _Pending] = {}

    def compose(self):
        """Yield a header label and the ListView that renders tool activity."""
        with Vertical():
            yield Label("Activity", classes="sidebar-header")
            yield ListView(id="activity-list")

    def pending_count(self) -> int:
        """Return the number of tool calls currently tracked as in flight."""
        return len(self._pending)

    def tool_started(self, call_id: str, name: str, args: dict) -> None:
        """Register a newly-requested tool call."""
        self._pending[call_id] = _Pending(name=name)
        if self.is_mounted:
            self._rebuild_list()

    def tool_finished(self, call_id: str, name: str, ok: bool, detail: str) -> None:
        """Mark a tool call as resolved and stop tracking it."""
        self._pending.pop(call_id, None)
        if self.is_mounted:
            self._rebuild_list()

    def _rebuild_list(self) -> None:
        """Repopulate the ListView from ``self._pending``.

        Named ``_rebuild_list``, deliberately not ``_render`` — ``Widget``
        already owns a private ``_render()`` (it computes the widget's own
        paintable ``Visual`` and is called directly by Textual's render
        pipeline). A same-named method here used to silently shadow it and made
        that internal call return ``None`` instead of a ``Visual``,
        crashing the whole app the instant this sidebar was ever actually
        painted on screen (``AttributeError: 'NoneType' object has no
        attribute 'render_strips'``) — masked for this entire session
        because a *different*, independent bug (``ChatPane`` missing an
        explicit ``width`` — see ``luna.tcss``) kept pushing this sidebar
        off-screen, where Textual never renders it and so never called the
        shadowed method. Confirmed empirically by bisecting a from-scratch
        copy of this class down to the single renamed method.
        """
        list_view = self.query_one("#activity-list", ListView)
        list_view.clear()
        for pending in self._pending.values():
            list_view.append(ListItem(PulseGlyph(pending.name, started_at=pending.started_at)))
