"""Live Markdown rendering for the assistant's streamed answer text.

Same technique real competitor CLIs and the documented Pydantic AI
streaming example use: accumulate the streamed text and re-render the
whole thing as ``rich.markdown.Markdown`` on a ``rich.live.Live`` region.
Rich's Markdown parser degrades gracefully on incomplete syntax (an
unclosed ``**`` or code fence briefly renders as plain text until the
closing delimiter arrives) — no flicker-inducing tricks needed. Unlike
:class:`luna.ui.progress.ToolProgress`, this ``Live`` is NOT transient:
when a segment finishes, the last rendered frame is exactly the answer,
so stopping the live leaves it on screen permanently instead of erasing
it.
"""

from __future__ import annotations

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown


class AnswerRenderer:
    """Renders one turn's streamed answer text as live-updating Markdown.

    A turn may produce more than one text segment (prose, then a tool call,
    then more prose) — call :meth:`stop` between segments to finalize the
    current one before a segment-free interlude (e.g. a tool's own progress
    line) and :meth:`append` again to start the next segment fresh.
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._live: Live | None = None
        self._text = ""

    def append(self, text: str) -> None:
        """Add streamed text to the current segment and refresh the display."""
        if not text:
            return
        self._text += text
        if self._live is None:
            self._live = Live(
                Markdown(self._text),
                console=self._console,
                transient=False,
                vertical_overflow="visible",
                refresh_per_second=10,
            )
            self._live.start()
        else:
            self._live.update(Markdown(self._text))

    def stop(self) -> None:
        """Finalize the current segment, if any; a no-op when nothing streamed."""
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._text = ""
