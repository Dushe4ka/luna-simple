import httpx
from langchain_core.messages import AIMessage

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.server.app import create_app
from luna.tui.app import LunaApp
from luna.tui.chat import ChatPane


def transcript_text(transcript) -> str:
    """Join every mounted message widget's ``.source`` in DOM order.

    ``#transcript`` holds one message widget per turn (UserMessage /
    LunaMessage) instead of one shared Markdown widget, so a plain
    ``.source`` lookup no longer exists on the container itself.
    """
    widgets = transcript.query("UserMessage, LunaMessage, SystemMessage")
    return "\n".join(child.source for child in widgets)


async def test_app_mounts_sidebar_chat_and_status_bar(tmp_path, fake_model):
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
        assert not app.query("#activity-sidebar")
        assert app.query_one("#status-bar") is not None


async def test_chat_input_has_focus_on_mount(tmp_path, fake_model):
    """Regression: Textual's AUTO_FOCUS picks the first focusable widget in
    DOM order, which is the sessions ListView (it mounts before ChatPane's
    Input) — not the input a chat app should open ready-to-type in.
    Without an explicit focus() call, every keystroke on launch silently
    lands in the session list instead of the chat input.
    """
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(AIMessage(content="ok")))
    app_asgi = create_app(agent_factory=lambda _workdir: agent, token="t")
    transport = httpx.ASGITransport(app=app_asgi)

    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id="t1")
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")

    async with app.run_test() as pilot:
        chat_input = app.query_one("#chat-input")
        assert app.focused is chat_input
        await pilot.press(*"hi")
        assert chat_input.value == "hi"


async def test_transcript_fills_available_height_so_input_is_bottom_anchored(tmp_path, fake_model):
    """Regression: Markdown's own default CSS is ``height: auto`` (sizes to
    content), and nothing overrode it — so with an empty transcript the
    whole chat column collapsed to ~3 rows and the input row sat right
    under the header instead of anchored to the bottom of the screen.
    ``#transcript`` must be ``1fr`` so it claims the remaining space.
    """
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(AIMessage(content="ok")))
    app_asgi = create_app(agent_factory=lambda _workdir: agent, token="t")
    transport = httpx.ASGITransport(app=app_asgi)

    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id="t1")
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")

    async with app.run_test():
        transcript = app.query_one("#transcript")
        chat_input = app.query_one("#chat-input")
        assert transcript.region.height > 0
        assert chat_input.region.y > 0
        # the input's bottom edge must sit above the status bar/footer, not
        # float mid-screen — i.e. it is the last thing in the chat column
        status_bar = app.query_one("#status-bar")
        assert chat_input.region.y + chat_input.region.height <= status_bar.region.y


async def test_status_bar_has_real_visible_content_height_not_just_a_border(tmp_path, fake_model):
    """Regression: `#status-bar`'s CSS combined `height: 1` with a
    `border-top` — Textual's box model counts a border *inside* the
    declared height, not on top of it, so the border consumed the widget's
    entire single row and left ZERO rows for the actual text. Checking
    only `.region.height` (as the test above does) does not catch this —
    that stayed a perfectly normal-looking 1 the whole time. Only
    `.size` — the interior content box — exposed it as 0, confirmed by
    running the real app through an actual pty (Textual's own headless
    test driver was not enough to be sure of this on its own).
    """
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(AIMessage(content="ok")))
    app_asgi = create_app(agent_factory=lambda _workdir: agent, token="t")
    transport = httpx.ASGITransport(app=app_asgi)

    app = LunaApp(
        base_url="http://test", token="t", workdir=str(tmp_path), thread_id="t1", model="m"
    )
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")

    async with app.run_test():
        status_bar = app.query_one("#status-bar")
        assert status_bar.size.height > 0, "status bar's content box has no room for its text"


async def test_chat_fills_the_width_right_of_the_sessions_sidebar(tmp_path, fake_model):
    """Regression guard for the old `width: 1fr` bug: ChatPane must share the
    row with the sidebar and end exactly at the terminal's right edge."""
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(AIMessage(content="ok")))
    app_asgi = create_app(agent_factory=lambda _workdir: agent, token="t")
    transport = httpx.ASGITransport(app=app_asgi)

    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id="t1")
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")

    async with app.run_test(size=(120, 40)):
        sidebar = app.query_one("#sessions-sidebar")
        chat = app.query_one(ChatPane)
        assert chat.region.x == sidebar.region.x + sidebar.region.width
        assert chat.region.x + chat.region.width == 120


async def test_chat_transcript_shows_prior_history_on_mount(tmp_path):
    """Regression: opening the TUI on an existing thread showed a blank
    transcript until the NEXT new message — the prior conversation never
    appeared at all.
    """
    from types import SimpleNamespace

    class _StubAgent:
        def get_state(self, config):
            return SimpleNamespace(
                values={
                    "messages": [
                        SimpleNamespace(type="human", content="what does this repo do"),
                        SimpleNamespace(type="ai", content="it parses the config file"),
                    ]
                }
            )

    app_asgi = create_app(agent_factory=lambda _workdir: _StubAgent(), token="t")
    transport = httpx.ASGITransport(app=app_asgi)

    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id="t1")
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")

    async with app.run_test():
        transcript = app.query_one("#transcript")
        text = transcript_text(transcript)
        assert "what does this repo do" in text
        assert "it parses the config file" in text


async def test_switching_sessions_reloads_that_threads_history(tmp_path):
    """Regression: clicking a different session in the sidebar swapped
    ``thread_id`` but kept showing whatever the PREVIOUS session had
    streamed into the transcript — nothing reloaded the new thread's own
    history, and nothing cleared the old one out.
    """
    from types import SimpleNamespace

    from luna.tui.sidebar_sessions import SessionsSidebar

    class _StubAgent:
        def get_state(self, config):
            thread_id = config["configurable"]["thread_id"]
            text = "session one" if thread_id == "t1" else "session two"
            return SimpleNamespace(values={"messages": [SimpleNamespace(type="ai", content=text)]})

    app_asgi = create_app(agent_factory=lambda _workdir: _StubAgent(), token="t")
    transport = httpx.ASGITransport(app=app_asgi)

    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id="t1")
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")

    async with app.run_test() as pilot:
        transcript = app.query_one("#transcript")
        assert "session one" in transcript_text(transcript)

        sidebar = app.query_one(SessionsSidebar)
        sidebar.post_message(SessionsSidebar.SessionSelected("t2"))
        await pilot.pause()

        assert app.query_one(ChatPane).thread_id == "t2"
        text = transcript_text(transcript)
        assert "session two" in text
        assert "session one" not in text


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


async def test_server_refusal_is_shown_instead_of_crashing_the_tui(tmp_path):
    """Regression: any 4xx from the server raised HTTPStatusError out of
    on_mount and killed the TUI with a traceback."""
    from textual.containers import VerticalScroll
    from textual.widgets import Input

    app_asgi = create_app(agent_factory=lambda _w: None, token="t", trust_check=lambda _w: False)
    transport = httpx.ASGITransport(app=app_asgi)
    app = LunaApp(base_url="http://test", token="t", workdir=str(tmp_path), thread_id="t1")
    app.client._http = httpx.AsyncClient(transport=transport, base_url="http://test")

    async with app.run_test() as pilot:
        chat = app.query_one(ChatPane)
        inp = chat.query_one("#chat-input", Input)
        await chat.on_input_submitted(Input.Submitted(inp, "hello"))
        await pilot.press("ctrl+n")
        await pilot.pause()
        text = transcript_text(chat.query_one("#transcript", VerticalScroll))
        assert "не отмечена как доверенная" in text
    assert app.return_code in (None, 0)
