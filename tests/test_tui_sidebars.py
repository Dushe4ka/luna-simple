import time

import httpx
from textual.widgets import Input, Label

from luna.core.store import open_log
from luna.tui.app import LunaApp
from luna.tui.chat import ChatPane
from luna.tui.sidebar_sessions import SessionsSidebar, short_path
from tests.server_helpers import StreamingASGITransport, make_app, seed


def _app(tmp_path, thread_id="t1") -> LunaApp:
    transport = StreamingASGITransport(make_app())
    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id=thread_id)
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")
    return app


def _age(thread_id: str, seconds: float) -> None:
    log = open_log()
    with log._lock:
        log._conn.execute(
            "UPDATE sessions SET updated = ? WHERE id = ?", (time.time() - seconds, thread_id)
        )


def test_short_path_uses_tilde_and_truncates_from_the_left(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert short_path(str(tmp_path / "a" / "b"), 30) == "~/a/b"
    long = short_path(str(tmp_path / ("x" * 50)), 20)
    assert len(long) == 20 and long.startswith("…")


async def test_sidebar_groups_sessions_and_skips_empty_groups(tmp_path):
    seed(open_log(), tmp_path, "fix the bug in toolguard", session_id="t1")
    seed(open_log(), tmp_path, "old question", session_id="t2")
    _age("t2", 30 * 86400)
    app = _app(tmp_path)
    async with app.run_test():
        sidebar = app.query_one(SessionsSidebar)
        await sidebar.refresh_sessions()
        groups = [str(item.query_one(Label).render()) for item in sidebar.query(".session-group")]
        assert groups == ["СЕГОДНЯ", "РАНЕЕ"]
        assert all(item.disabled for item in sidebar.query(".session-group"))
        titles = [item.data_thread_id for item in sidebar.query(".session-item")]
        assert titles == ["t1", "t2"]


async def test_current_session_is_highlighted_and_follows_set_current(tmp_path):
    seed(open_log(), tmp_path, "one", session_id="t1")
    seed(open_log(), tmp_path, "two", session_id="t2")
    app = _app(tmp_path, thread_id="t1")
    async with app.run_test():
        sidebar = app.query_one(SessionsSidebar)
        await sidebar.refresh_sessions()
        current = [i.data_thread_id for i in sidebar.query(".session-item.-current")]
        assert current == ["t1"]
        sidebar.set_current("t2")
        current = [i.data_thread_id for i in sidebar.query(".session-item.-current")]
        assert current == ["t2"]


async def test_ctrl_n_starts_a_fresh_session(tmp_path):
    app = _app(tmp_path, thread_id="t1")
    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        chat = app.query_one(ChatPane)
        assert chat.thread_id not in (None, "t1")
        assert app.query_one(SessionsSidebar).current_thread_id == chat.thread_id


async def test_sidebar_shows_the_session_after_its_first_turn(tmp_path):
    app = _app(tmp_path, thread_id="t-new")
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        chat.query_one("#chat-input", Input)
        await chat.submit("hello")
        await pilot.pause()
        ids = [i.data_thread_id for i in app.query_one(SessionsSidebar).query(".session-item")]
        assert "t-new" in ids


async def test_header_shows_the_real_folder_name_for_a_relative_workdir(tmp_path, monkeypatch):
    """Regression: the CLI passes `workdir="."`, which rendered as "◐ ." / "."."""
    project = tmp_path / "Luna_pi"
    project.mkdir()
    monkeypatch.chdir(project)
    app = _app(project)
    app._workdir = "."
    async with app.run_test():
        sidebar = app.query_one(SessionsSidebar)
        name = str(sidebar.query_one(".project-name", Label).render())
        assert name == "◐ Luna_pi"


async def test_switching_sessions_is_blocked_while_a_turn_is_running(tmp_path):
    app = _app(tmp_path, thread_id="t1")
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        chat.busy = True
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert chat.thread_id == "t1"
        # no orphan session created (t1 itself was opened by the history request)
        assert [i.id for i in open_log().list(str(tmp_path.resolve()))] == ["t1"]
