"""Luna's full-screen Textual TUI application."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer

from luna.server.client import ServerClient
from luna.tui.chat import ChatPane
from luna.tui.sidebar_activity import ActivitySidebar
from luna.tui.sidebar_sessions import SessionsSidebar
from luna.tui.status_bar import StatusBar
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

    def __init__(self, *, base_url: str, token: str, workdir: str, thread_id: str) -> None:
        super().__init__()
        self._workdir = workdir
        # The CLI already resolved the thread to start on (a fresh uuid4 hex,
        # or the one picked by --resume/--continue); the TUI must open on
        # THAT thread. Passing it down to ChatPane as a required constructor
        # argument — rather than leaving the reactive at its None default and
        # hoping the user clicks a session — is what keeps every turn from
        # landing on a literal graph thread named "None".
        #
        # NB: *not* `self._thread_id` — Textual's MessagePump already owns
        # that attribute (it re-stamps it with `threading.get_ident()` when
        # the app starts running), so storing the graph thread there would be
        # silently overwritten with an OS thread id before compose() runs.
        self._start_thread_id = thread_id
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
            yield ChatPane(workdir=self._workdir, thread_id=self._start_thread_id)
            yield activity_sidebar
        yield StatusBar(id="status-bar")
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
