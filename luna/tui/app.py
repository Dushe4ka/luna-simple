"""Luna's full-screen Textual TUI application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Static

from luna.server.client import ServerClient
from luna.tui.chat import ChatPane
from luna.tui.theme import TUI_CSS_VARIABLES


class LunaApp(App):
    """The full-screen Luna TUI — a thin client over the local server."""

    CSS = (
        TUI_CSS_VARIABLES
        + """
    #sessions-sidebar {
        width: 24;
        background: $bg;
        border-right: solid $border;
    }
    #activity-sidebar {
        width: 30;
        background: $bg;
        border-left: solid $border;
    }
    ChatPane {
        background: $bg;
    }
    #status-bar {
        height: 1;
        background: $bg;
        color: $moon-dim;
    }
    """
    )

    BINDINGS = [("ctrl+b", "toggle_panels", "Toggle panels")]

    def __init__(self, *, base_url: str, token: str, workdir: str) -> None:
        super().__init__()
        self._workdir = workdir
        self.client = ServerClient(base_url=base_url, token=token)

    def compose(self) -> ComposeResult:
        """Build the 3-zone layout: two sidebars, chat pane, status bar, footer."""
        with Horizontal():
            yield Static(id="sessions-sidebar")
            yield ChatPane(workdir=self._workdir)
            yield Static(id="activity-sidebar")
        yield Static(id="status-bar")
        yield Footer()

    def action_toggle_panels(self) -> None:
        """Show/hide the two sidebars (Ctrl+B)."""
        for widget_id in ("#sessions-sidebar", "#activity-sidebar"):
            widget = self.query_one(widget_id)
            widget.display = not widget.display

    async def on_unmount(self) -> None:
        """Close the server client's HTTP connection when the app exits."""
        await self.client.aclose()
