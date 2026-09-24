from luna.tui.app import LunaApp


async def test_app_mounts_three_zones():
    app = LunaApp(base_url="http://test", token="t", workdir=".")
    async with app.run_test() as _pilot:
        assert app.query_one("#sessions-sidebar") is not None
        assert app.query_one("#chat-pane") is not None
        assert app.query_one("#activity-sidebar") is not None
        assert app.query_one("#status-bar") is not None
