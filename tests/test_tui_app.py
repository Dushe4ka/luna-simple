import httpx
from langchain_core.messages import AIMessage

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.server.app import create_app
from luna.tui.app import LunaApp
from luna.tui.chat import ChatPane


async def test_app_mounts_three_zones(tmp_path, fake_model):
    # Task 9 wired `SessionsSidebar.refresh_sessions()` into `on_mount`, so
    # mounting the app now makes a real request through `app.client` — back
    # it with an in-process ASGI transport (same pattern as
    # tests/test_tui_sidebars.py) instead of hitting the network.
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(AIMessage(content="ok")))
    app_asgi = create_app(agent_factory=lambda _workdir: agent, token="t")
    transport = httpx.ASGITransport(app=app_asgi)

    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id="t1")
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")

    async with app.run_test() as _pilot:
        assert app.query_one("#sessions-sidebar") is not None
        # Task 8 replaced the `#chat-pane` placeholder Static with the real
        # ChatPane widget, which (per its verbatim reference code) does not
        # carry that id itself — address it by type instead.
        assert app.query_one(ChatPane) is not None
        assert app.query_one("#activity-sidebar") is not None
        assert app.query_one("#status-bar") is not None


async def test_chat_pane_opens_on_the_thread_id_the_cli_resolved(tmp_path, fake_model):
    """Regression (C1+C6): by the time the app is interactive, ChatPane must
    already address the thread the CLI resolved — a fresh uuid4 hex, or the
    one --resume/--continue picked. It used to stay at its ``None`` reactive
    default unless the user clicked a session, so every fresh TUI turn went
    to a graph thread literally named "None" and --resume was discarded.
    """
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(AIMessage(content="ok")))
    app_asgi = create_app(agent_factory=lambda _workdir: agent, token="t")
    transport = httpx.ASGITransport(app=app_asgi)

    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id="abc123")
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")

    async with app.run_test():
        assert app.query_one(ChatPane).thread_id == "abc123"
