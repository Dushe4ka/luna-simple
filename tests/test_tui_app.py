from luna.tui.app import LunaApp
from luna.tui.chat import ChatPane


async def test_app_mounts_three_zones():
    app = LunaApp(base_url="http://test", token="t", workdir=".")
    async with app.run_test() as _pilot:
        assert app.query_one("#sessions-sidebar") is not None
        # Task 8 replaced the `#chat-pane` placeholder Static with the real
        # ChatPane widget, which (per its verbatim reference code) does not
        # carry that id itself — address it by type instead.
        assert app.query_one(ChatPane) is not None
        assert app.query_one("#activity-sidebar") is not None
        assert app.query_one("#status-bar") is not None
