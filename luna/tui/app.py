"""Luna's full-screen Textual TUI application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Static

from luna.server.client import ServerClient
from luna.tui.chat import ChatPane
from luna.tui.sidebar_activity import ActivitySidebar
from luna.tui.sidebar_sessions import SessionsSidebar
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
        # SessionsSidebar/ActivitySidebar's __init__ signatures (verbatim from
        # the task brief) don't forward an `id=` kwarg to Widget.__init__, so
        # the id is assigned on the instance instead — DOMNode.id is settable
        # once, before mount — to keep matching the existing
        # #sessions-sidebar/#activity-sidebar CSS selectors and the Task 7
        # widget-lookup test.
        sessions_sidebar = SessionsSidebar(workdir=self._workdir)
        sessions_sidebar.id = "sessions-sidebar"
        activity_sidebar = ActivitySidebar()
        activity_sidebar.id = "activity-sidebar"
        with Horizontal():
            yield sessions_sidebar
            yield ChatPane(workdir=self._workdir)
            yield activity_sidebar
        yield Static(id="status-bar")
        yield Footer()

    async def on_mount(self) -> None:
        """Populate the session list from the server on startup."""
        await self.query_one(SessionsSidebar).refresh_sessions()

    def on_sessions_sidebar_session_selected(self, event: SessionsSidebar.SessionSelected) -> None:
        """Switch the chat pane to the clicked session's thread."""
        self.query_one(ChatPane).thread_id = event.thread_id

    def action_toggle_panels(self) -> None:
        """Show/hide the two sidebars (Ctrl+B)."""
        for widget_id in ("#sessions-sidebar", "#activity-sidebar"):
            widget = self.query_one(widget_id)
            widget.display = not widget.display

    async def on_unmount(self) -> None:
        """Close the server client's HTTP connection when the app exits."""
        await self.client.aclose()
