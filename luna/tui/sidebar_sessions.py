"""Left sidebar: session list, auto-named, with relative last-activity time."""

from __future__ import annotations

from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView


class SessionsSidebar(Widget):
    """Lists sessions for the current workdir; posts SessionSelected on click."""

    class SessionSelected(Message):
        """Posted when the user clicks a session in the list."""

        def __init__(self, thread_id: str) -> None:
            self.thread_id = thread_id
            super().__init__()

    def __init__(self, *, workdir: str) -> None:
        super().__init__()
        self._workdir = workdir

    def compose(self):
        """Yield the ListView that renders the session list."""
        yield ListView(id="session-list")

    async def refresh_sessions(self) -> None:
        """Fetch the session list from the server and repopulate the list view."""
        sessions = await self.app.client.list_sessions(self._workdir)
        list_view = self.query_one("#session-list", ListView)
        list_view.clear()
        for s in sessions:
            item = ListItem(Label(f"{s['title']}  ·  {s['relative_time']}"))
            item.data_thread_id = s["thread_id"]  # plain attribute, no reactive needed here
            await list_view.append(item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Post SessionSelected for the clicked item's thread_id, if any."""
        thread_id = getattr(event.item, "data_thread_id", None)
        if thread_id:
            self.post_message(self.SessionSelected(thread_id))
