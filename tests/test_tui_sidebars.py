import time

import httpx
from langchain_core.messages import AIMessage
from textual.widgets import Input, Label

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.persistence import SessionIndex
from luna.server.app import create_app
from luna.tui.app import LunaApp
from luna.tui.chat import ChatPane
from luna.tui.sidebar_sessions import SessionsSidebar, short_path


def _app(tmp_path, fake_model, thread_id="t1") -> LunaApp:
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(AIMessage(content="ok")))
    transport = httpx.ASGITransport(app=create_app(agent_factory=lambda _w: agent, token="t"))
    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id=thread_id)
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")
    return app


def _age(thread_id: str, seconds: float) -> None:
    idx = SessionIndex()
    idx._conn.execute(
        "UPDATE luna_sessions SET updated = ? WHERE thread_id = ?",
        (time.time() - seconds, thread_id),
    )
    idx._conn.commit()


def test_short_path_uses_tilde_and_truncates_from_the_left(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert short_path(str(tmp_path / "a" / "b"), 30) == "~/a/b"
    long = short_path(str(tmp_path / ("x" * 50)), 20)
    assert len(long) == 20 and long.startswith("…")


async def test_sidebar_groups_sessions_and_skips_empty_groups(tmp_path, fake_model):
    idx = SessionIndex()
    idx.record("t1", str(tmp_path), "fix the bug in toolguard")
    idx.record("t2", str(tmp_path), "old question")
    _age("t2", 30 * 86400)
    app = _app(tmp_path, fake_model)
    async with app.run_test():
        sidebar = app.query_one(SessionsSidebar)
        await sidebar.refresh_sessions()
        groups = [str(item.query_one(Label).render()) for item in sidebar.query(".session-group")]
        assert groups == ["СЕГОДНЯ", "РАНЕЕ"]
        assert all(item.disabled for item in sidebar.query(".session-group"))
        titles = [item.data_thread_id for item in sidebar.query(".session-item")]
        assert titles == ["t1", "t2"]


async def test_current_session_is_highlighted_and_follows_set_current(tmp_path, fake_model):
    idx = SessionIndex()
    idx.record("t1", str(tmp_path), "one")
    idx.record("t2", str(tmp_path), "two")
    app = _app(tmp_path, fake_model, thread_id="t1")
    async with app.run_test():
        sidebar = app.query_one(SessionsSidebar)
        await sidebar.refresh_sessions()
        current = [i.data_thread_id for i in sidebar.query(".session-item.-current")]
        assert current == ["t1"]
        sidebar.set_current("t2")
        current = [i.data_thread_id for i in sidebar.query(".session-item.-current")]
        assert current == ["t2"]


async def test_ctrl_n_starts_a_fresh_session(tmp_path, fake_model):
    app = _app(tmp_path, fake_model, thread_id="t1")
    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        chat = app.query_one(ChatPane)
        assert chat.thread_id not in (None, "t1")
        assert app.query_one(SessionsSidebar).current_thread_id == chat.thread_id


async def test_sidebar_shows_the_session_after_its_first_turn(tmp_path, fake_model):
    app = _app(tmp_path, fake_model, thread_id="t-new")
    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "hello"))
        await pilot.pause()
        ids = [i.data_thread_id for i in app.query_one(SessionsSidebar).query(".session-item")]
        assert "t-new" in ids
