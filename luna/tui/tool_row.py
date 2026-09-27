"""One tool call inline in the chat transcript, Claude-Code style.

While running it is a twinkling ``PulseGlyph``; once finished it becomes a
coloured ``●`` headline plus a ``└ detail · 1.2s`` line. Replayed history
rows are built finished (``result=``) and have no duration, which is not
stored.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static

from luna.tui.theme import TUI_VARIABLES
from luna.tui.widgets import PulseGlyph


class ToolRow(Vertical):
    """A tool call's row: pulsing while running, ``●`` + detail when done."""

    def __init__(self, name: str, preview: str, *, result: tuple[bool, str] | None = None) -> None:
        super().__init__()
        self.tool_name = name
        self.preview = preview
        self._started_at = time.monotonic()
        self._result = result
        self._duration: str | None = None

    @property
    def is_finished(self) -> bool:
        """True once the call has an outcome (live or replayed)."""
        return self._result is not None

    def _label(self) -> str:
        return f"{self.tool_name}  «{self.preview}»" if self.preview else self.tool_name

    def compose(self):
        """Yield the pulsing label while running, else the result lines."""
        if self._result is None:
            yield PulseGlyph(self._label())
            return
        ok, detail = self._result
        head = Text()
        head.append("● ", style=TUI_VARIABLES["ok"] if ok else TUI_VARIABLES["err"])
        head.append(self.tool_name, style="bold")
        if self.preview:
            head.append(f"  «{self.preview}»", style=TUI_VARIABLES["moon-dim"])
        yield Static(head, classes="tool-head")
        tail = " · ".join(part for part in (detail, self._duration) if part)
        if tail:
            yield Static(f"  └ {tail}", classes="tool-detail")

    async def finish(self, ok: bool, detail: str) -> None:
        """Record the outcome and duration, then swap the pulse for the result."""
        self._duration = f"{time.monotonic() - self._started_at:.1f}s"
        self._result = (ok, detail)
        await self.recompose()
