"""Left sidebar: the current project's sessions, grouped by day."""

from __future__ import annotations

import os
from pathlib import Path

from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView

#: Client-side labels for the server's language-neutral ``group`` values.
GROUP_LABELS = {
    "today": "Сегодня",
    "yesterday": "Вчера",
    "week": "На этой неделе",
    "older": "Ранее",
}


def short_path(path: str, width: int) -> str:
    """Shorten ``$HOME`` to ``~`` and truncate from the left with ``…`` to ``width``."""
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        path = "~" + path[len(home) :]
    return path if len(path) <= width else "…" + path[-(width - 1) :]


class SessionsSidebar(Widget):
    """The open project's sessions; posts SessionSelected / NewSessionRequested.

    Styling lives in ``luna/tui/luna.tcss`` (external stylesheet), not a
    ``DEFAULT_CSS`` string here.
    """

    class SessionSelected(Message):
        """Posted when the user picks a session in the list."""

        def __init__(self, thread_id: str) -> None:
            self.thread_id = thread_id
            super().__init__()

    class NewSessionRequested(Message):
        """Posted when the user picks the "+ новая сессия" row."""

    def __init__(self, *, workdir: str, current_thread_id: str | None = None) -> None:
        super().__init__()
        self._workdir = workdir
        self.current_thread_id = current_thread_id

    def compose(self):
        """Yield the project header and the session list."""
        # Resolved for display only: the CLI may pass a relative "." here.
        resolved = Path(self._workdir).resolve()
        with Vertical():
            yield Label(f"◐ {resolved.name or resolved}", classes="project-name")
            yield Label(short_path(str(resolved), 28), classes="project-path")
            yield ListView(id="session-list")

    async def refresh_sessions(self) -> None:
        """Fetch this project's sessions and rebuild the grouped list.

        Group headers are disabled ``ListItem``s: ``ListView`` skips disabled
        items when moving the cursor, and they cannot be selected.
        """
        sessions = await self.app.client.list_sessions(self._workdir)
        list_view = self.query_one("#session-list", ListView)
        await list_view.clear()
        items: list[ListItem] = [ListItem(_NewSessionRow(), classes="new-session-item")]
        group = None
        for s in sessions:
            if s["group"] != group:
                group = s["group"]
                items.append(
                    ListItem(
                        Label(GROUP_LABELS[group].upper()),
                        classes="session-group",
                        disabled=True,
                    )
                )
            item = ListItem(_SessionRow(s["title"], s["relative_time"]), classes="session-item")
            item.data_thread_id = s["thread_id"]  # plain attribute, no reactive needed here
            item.set_class(s["thread_id"] == self.current_thread_id, "-current")
            items.append(item)
        await list_view.extend(items)

    def set_current(self, thread_id: str) -> None:
        """Move the ``-current`` highlight to ``thread_id``'s row."""
        self.current_thread_id = thread_id
        for item in self.query(".session-item"):
            item.set_class(item.data_thread_id == thread_id, "-current")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Post NewSessionRequested or SessionSelected for the picked row."""
        if event.item.has_class("new-session-item"):
            self.post_message(self.NewSessionRequested())
            return
        thread_id = getattr(event.item, "data_thread_id", None)
        if thread_id:
            self.post_message(self.SessionSelected(thread_id))


class _SessionRow(Horizontal):
    """One line: ellipsis-truncated title, relative time right-aligned."""

    def __init__(self, title: str, relative_time: str) -> None:
        super().__init__()
        self._title = title
        self._relative_time = relative_time

    def compose(self):
        yield Label(self._title, classes="session-title")
        yield Label(self._relative_time, classes="session-time")


class _NewSessionRow(Horizontal):
    """The "+ новая сессия ^n" row at the top of the list."""

    def compose(self):
        yield Label("+ новая сессия", classes="session-title")
        yield Label("^n", classes="session-time")
