"""TUI: a ``need_key`` command effect opens a masked key modal."""

from luna_core.client import ServerError
from textual.widgets import Input, Static

from luna.tui.app import LunaApp
from luna.tui.chat import ChatPane, NoticeRow
from luna.tui.key_modal import KeyModal
from tests.tui_fakes import FakeLunaClient

SECRET = "sk-ant-TUI-SECRET-77"


def _res(**kw):
    base = {"text": "", "notices": [], "choice": None, "confirm": None, "prompt": None}
    base["effects"] = {}
    base.update(kw)
    return base


NEED = _res(
    notices=[{"level": "warn", "text": "нет ключа для anthropic"}],
    effects={
        "need_key": {
            "provider": "anthropic",
            "env_var": "ANTHROPIC_API_KEY",
            "hint": "Где взять ключ: https://console.anthropic.com/settings/keys",
            "path": "~/.config/luna/credentials.toml",
        }
    },
)
SWITCHED = _res(
    notices=[{"level": "info", "text": "provider → anthropic"}],
    effects={"provider": "anthropic"},
)


def _client():
    client = FakeLunaClient()
    client.command_results["/provider anthropic"] = [NEED, SWITCHED]
    return client


def _app(client):
    app = LunaApp(base_url="http://x", token="t", workdir="/w", thread_id="s1")
    app.client = client
    return app


async def _open_modal(app, pilot):
    chat = app.query_one(ChatPane)
    chat.query_one("#chat-input", Input).focus()
    await pilot.press(*"/provider anthropic", "enter")
    await pilot.pause(0.3)
    assert isinstance(app.screen, KeyModal)
    return chat


def _everything_shown(app) -> str:
    chat = app.query_one(ChatPane)
    parts = [str(n.message) for n in app._notifications]
    parts += [r.text for r in chat.query(NoticeRow)]
    parts += [w.source for w in chat.query("UserMessage, SystemMessage")]
    return "\n".join(parts)


async def test_modal_is_masked_and_explains_where_the_key_goes():
    app = _app(_client())
    async with app.run_test(size=(130, 36)) as pilot:
        await _open_modal(app, pilot)
        modal = app.screen
        key_input = modal.query_one("#key-input", Input)
        assert key_input.password is True
        assert key_input.has_focus
        text = " ".join(str(s.render()) for s in modal.query(Static))
        assert "Ключ для anthropic" in text
        assert "ANTHROPIC_API_KEY" in text
        assert "credentials.toml" in text and "0600" in text
        assert "console.anthropic.com" in text
        await pilot.press("escape")
        await pilot.pause(0.2)


async def test_save_stores_the_key_and_reruns_the_switch():
    client = _client()
    app = _app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await _open_modal(app, pilot)
        await pilot.press(*SECRET, "enter")
        await pilot.pause(0.4)
        assert not isinstance(app.screen, KeyModal)
        assert client.keys_set == [("anthropic", "/w")]
        assert client.last_key_len == len(SECRET)
        assert client.command_lines == ["/provider anthropic", "/provider anthropic"]
        shown = _everything_shown(app)
        assert "Ключ сохранён, провайдер → anthropic" in shown
        assert SECRET not in shown
        assert not any(SECRET in str(v) for v in vars(app.query_one(ChatPane)).values())


async def test_escape_cancels_without_saving():
    client = _client()
    app = _app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await _open_modal(app, pilot)
        await pilot.press(*"sk-abc", "escape")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, KeyModal)
        assert client.keys_set == []
        assert client.command_lines == ["/provider anthropic"]
        assert "sk-abc" not in _everything_shown(app)


async def test_empty_input_shows_a_hint_and_stays_open():
    client = _client()
    app = _app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await _open_modal(app, pilot)
        await pilot.press("space", "enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, KeyModal)
        error = app.screen.query_one("#key-error", Static)
        assert "пуст" in str(error.render()).lower()
        assert client.keys_set == []
        await pilot.press("escape")
        await pilot.pause(0.2)


async def test_server_error_is_shown_inline_in_russian_and_modal_stays():
    client = _client()
    client.set_key_error = ServerError("HTTP 400: unknown_provider", 400)
    app = _app(client)
    async with app.run_test(size=(130, 36)) as pilot:
        await _open_modal(app, pilot)
        await pilot.press(*SECRET, "enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, KeyModal)
        error = str(app.screen.query_one("#key-error", Static).render())
        assert "Неизвестный провайдер" in error
        assert SECRET not in error
        assert client.command_lines == ["/provider anthropic"]
        await pilot.press("escape")
        await pilot.pause(0.2)
