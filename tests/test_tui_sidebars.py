import pytest

from luna.tui.app import LunaApp


@pytest.mark.asyncio
async def test_sessions_sidebar_lists_sessions_from_the_server(tmp_path, monkeypatch, fake_model):
    import httpx
    from langchain_core.messages import AIMessage

    from luna.config.config import LunaConfig
    from luna.core.agent import build_agent
    from luna.core.persistence import SessionIndex
    from luna.server.app import create_app

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    SessionIndex().record("t1", str(tmp_path), "fix the bug in toolguard")

    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(AIMessage(content="ok")))
    app_asgi = create_app(agent_factory=lambda _workdir: agent, token="t")
    transport = httpx.ASGITransport(app=app_asgi)

    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id="t1")
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")

    async with app.run_test():
        from luna.tui.sidebar_sessions import SessionsSidebar

        sidebar = app.query_one(SessionsSidebar)
        await sidebar.refresh_sessions()
        items = list(sidebar.query("ListItem"))
        assert len(items) == 1
        # Regression: a single combined "title  ·  time" Label hard-clipped
        # mid-word inside the 24-column sidebar with no ellipsis, silently
        # dropping the trailing relative-time on any long title. The title
        # and time are now two separate, independently-styled rows.
        from luna.tui.sidebar_sessions import _SessionRow

        row = sidebar.query_one(_SessionRow)
        assert row._title == "fix the bug in toolguard"
        assert row._relative_time != ""


@pytest.mark.asyncio
async def test_activity_sidebar_adds_and_resolves_a_tool_entry():
    from luna.tui.sidebar_activity import ActivitySidebar

    sidebar = ActivitySidebar()
    sidebar.tool_started("1", "read_file", {"file_path": "/a.py"})
    assert sidebar.pending_count() == 1
    sidebar.tool_finished("1", "read_file", True, "")
    assert sidebar.pending_count() == 0


def test_activity_sidebar_does_not_shadow_widgets_private_render_method():
    """Regression: this class's own list-refresh helper used to be named
    ``_render`` — the exact same name as ``textual.widget.Widget``'s own
    private ``_render()``, which Textual's rendering pipeline calls
    directly to get the widget's paintable ``Visual``. The override made
    that internal call return ``None`` (this helper's own return value)
    instead of a ``Visual``, crashing the whole app — but *only* once this
    sidebar was actually laid out on screen, which a `run_test()` headless
    Pilot session never triggers (confirmed empirically: calling
    `tool_started` and even a forced `refresh(layout=True)` under
    `run_test()` never reproduced it — only a real pty did) and neither did
    the test above, since `is_mounted` is False for a bare, un-added
    instance. This static identity check is what actually catches it, at
    plain import time, with no widget mounting or real terminal needed.
    """
    from textual.widget import Widget

    from luna.tui.sidebar_activity import ActivitySidebar

    assert ActivitySidebar._render is Widget._render
