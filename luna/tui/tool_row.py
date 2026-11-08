"""One tool call inline in the chat transcript, Claude-Code style.

While running it is a twinkling ``PulseGlyph``; once finished it becomes a
``✓``/``✗`` headline (status by mark *and* theme color, never color alone)
plus a ``└ detail · 1.2s`` line. The row carries ``-running``/``-ok``/``-err``
so ``luna.tcss`` colors its left bar. Replayed history
rows are built finished (``result=``) and have no duration, which is not
stored.
"""

from __future__ import annotations

import time

from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Static

from luna.tui.widgets import PulseGlyph


class ToolRow(Vertical):
    """A tool call's row: pulsing while running, ``✓``/``✗`` + detail when done."""

    def __init__(self, name: str, preview: str, *, result: tuple[bool, str] | None = None) -> None:
        super().__init__()
        self.tool_name = name
        self.preview = preview
        self._started_at = time.monotonic()
        self._result = result
        self._duration: str | None = None
        self._sync_classes()

    def _sync_classes(self) -> None:
        ok = self._result[0] if self._result is not None else None
        self.set_class(ok is None, "-running")
        self.set_class(ok is True, "-ok")
        self.set_class(ok is False, "-err")

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
        # Assembled from literal parts, never markup: escape() leaves ``[$var]``
        # tags alone, so a command like ``echo ${arr[$i]}`` would be recolored.
        # The ``$ok``/``$muted`` styles resolve at render time, so they follow
        # theme switches.
        head = Content.assemble(
            ("✓", "$ok") if ok else ("✗", "$err"),
            " ",
            (self.tool_name, "bold"),
            ("  «" + self.preview + "»", "$muted") if self.preview else "",
        )
        yield Static(head, classes="tool-head")
        tail = " · ".join(part for part in (detail, self._duration) if part)
        if tail:
            yield Static(f"  └ {tail}", classes="tool-detail", markup=False)

    async def finish(self, ok: bool, detail: str) -> None:
        """Record the outcome and duration, then swap the pulse for the result."""
        self._duration = f"{time.monotonic() - self._started_at:.1f}s"
        self._result = (ok, detail)
        self._sync_classes()
        await self.recompose()
