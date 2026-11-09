"""/model across providers: grouped picker, filter, and the choice kept for the next start."""

import pytest

from luna.commands import run_line
from luna.commands.base import GROUP_HEADER
from luna.config.config import (
    LunaConfig,
    config_path,
    load_config,
    remember_model,
    set_config_values,
)
from luna.config.providers import ProviderSpec
from luna.turn.engine import Notice
from tests.test_command_registry import FakeEnv

_RICH_CONFIG = """# my settings
[model]
provider = "mylocal"  # local vLLM

[provider.custom.mylocal]
base_url = "http://127.0.0.1:8000/v1"

[[hooks]]
event = "post_tool"
command = "true"
"""


def _write_config(text: str) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# -- the config writer keeps what it does not touch ------------------------


def test_set_config_values_keeps_custom_providers_hooks_and_comments():
    _write_config(_RICH_CONFIG)
    set_config_values({"ui.theme": "daymoon"})
    text = config_path().read_text()
    assert "# my settings" in text and "# local vLLM" in text
    cfg = load_config()
    assert cfg.theme == "daymoon"
    assert cfg.custom_providers["mylocal"].base_url == "http://127.0.0.1:8000/v1"
    assert cfg.hooks == [{"event": "post_tool", "command": "true"}]


def test_remember_model_sets_both_and_a_provider_switch_drops_the_old_model():
    remember_model("openai", "gpt-4o")
    cfg = load_config()
    assert (cfg.provider, cfg.model) == ("openai", "gpt-4o")
    remember_model("anthropic", None)
    cfg = load_config()
    assert (cfg.provider, cfg.model) == ("anthropic", None)


def test_remember_model_accepts_the_users_own_custom_provider():
    _write_config(_RICH_CONFIG)
    remember_model("mylocal", "qwen3-coder")
    cfg = load_config()
    assert (cfg.provider, cfg.model) == ("mylocal", "qwen3-coder")
    assert cfg.hooks  # untouched


def test_remember_model_on_a_broken_config_raises_a_config_error():
    from luna.config.providers import LunaConfigError

    _write_config("[model\nprovider = ")
    with pytest.raises(LunaConfigError):
        remember_model("openai", "gpt-4o")


# -- /model and /provider remember the choice ------------------------------


async def test_model_switch_is_remembered_for_the_next_start():
    env = FakeEnv(cfg=LunaConfig(provider="deepseek"))
    result = await run_line("/model deepseek-reasoner", env)
    assert result.notices == [Notice("info", "model → deepseek-reasoner")]
    cfg = load_config()
    assert (cfg.provider, cfg.model) == ("deepseek", "deepseek-reasoner")


async def test_model_with_a_provider_switches_both_at_once(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    env = FakeEnv(cfg=LunaConfig(provider="deepseek", model="deepseek-chat"))
    result = await run_line("/model openai gpt-4o", env)
    assert result.notices == [Notice("info", "model → openai · gpt-4o")]
    assert result.effects == {"model": "gpt-4o", "provider": "openai"}
    assert (env.cfg.provider, env.cfg.model) == ("openai", "gpt-4o")
    assert env.rebuilt == 1  # one rebuild, not provider then model
    cfg = load_config()
    assert (cfg.provider, cfg.model) == ("openai", "gpt-4o")


async def test_model_with_a_provider_without_key_asks_for_it(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = FakeEnv(cfg=LunaConfig(provider="deepseek"))
    result = await run_line("/model openai gpt-4o", env)
    assert result.effects["need_key"]["provider"] == "openai"
    assert env.cfg.provider == "deepseek"
    assert not config_path().exists()


async def test_failed_switch_is_not_remembered():
    env = FakeEnv(cfg=LunaConfig(provider="deepseek"), fail_rebuild=RuntimeError("bad"))
    await run_line("/model nope", env)
    assert not config_path().exists()


async def test_provider_switch_is_remembered_without_the_old_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    remember_model("deepseek", "deepseek-chat")
    env = FakeEnv(cfg=LunaConfig(provider="deepseek", model="deepseek-chat"))
    await run_line("/provider openai", env)
    cfg = load_config()
    assert (cfg.provider, cfg.model) == ("openai", None)


async def test_a_project_provider_is_not_written_to_the_user_config():
    spec = ProviderSpec(
        key="proj",
        api="openai_chat",
        default_model="m",
        env_var=None,
        base_url="http://x/v1",
        project=True,
    )
    env = FakeEnv(cfg=LunaConfig(provider="proj", custom_providers={"proj": spec}))
    result = await run_line("/model other", env)
    assert result.notices == [Notice("info", "model → other")]
    assert not config_path().exists()


async def test_an_unwritable_config_warns_but_keeps_the_switch(monkeypatch):
    from luna.config import config as config_mod

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(config_mod, "remember_model", boom)
    env = FakeEnv(cfg=LunaConfig(provider="deepseek"))
    result = await run_line("/model deepseek-reasoner", env)
    assert env.cfg.model == "deepseek-reasoner"
    assert result.notices[-1].level == "warn" and "read-only" in result.notices[-1].text


# -- which providers the picker lists ---------------------------------------


def _stub_discovery(monkeypatch, live: dict, known: dict | None = None):
    from luna.commands import builtin
    from luna.config import model_discovery

    monkeypatch.setattr(builtin, "_api_key", lambda key, spec: "k" if key in live else None)
    monkeypatch.setattr(model_discovery, "list_models", lambda key, spec, **kw: live.get(key))
    monkeypatch.setattr(model_discovery, "known_models", lambda key: (known or {}).get(key, []))


def test_groups_list_connected_providers_current_first(monkeypatch):
    from luna.commands import builtin

    _stub_discovery(monkeypatch, {"openai": ["gpt-4o"], "deepseek": ["deepseek-chat"]})
    groups = builtin._model_groups(LunaConfig(provider="deepseek"))
    assert groups[0] == ("deepseek", ["deepseek-chat"])
    assert ("openai", ["gpt-4o"]) in groups
    assert "anthropic" not in dict(groups)  # no key


def test_groups_fall_back_to_known_models_and_hide_silent_keyless_ones(monkeypatch):
    from luna.commands import builtin

    # openai has a key but its API fails; ollama is keyless and not running
    _stub_discovery(monkeypatch, {"openai": None}, known={"openai": ["gpt-4o"], "ollama": ["x"]})
    groups = dict(builtin._model_groups(LunaConfig(provider="deepseek")))
    assert groups["openai"] == ["gpt-4o"]
    assert "ollama" not in groups


# -- TUI picker: headers and filter ------------------------------------------


def _model_options(n_openai: int = 12):
    return [
        (GROUP_HEADER, "deepseek"),
        ("deepseek deepseek-chat", "deepseek-chat"),
        (GROUP_HEADER, "openai"),
        *[(f"openai gpt-{i}", f"gpt-{i}") for i in range(n_openai)],
    ]


async def test_choice_modal_skips_headers_and_filters_as_you_type():
    from textual.widgets import Input, ListView

    from luna.tui.pickers import ChoiceModal
    from tests.tui_fakes import make_app

    app = make_app(theme_choice="moonlight")
    picked = []
    async with app.run_test() as pilot:
        app.push_screen(ChoiceModal("Модель", _model_options()), picked.append)
        await pilot.pause()
        listing = app.screen.query_one("#picker-list", ListView)
        assert app.screen.query_one("#picker-filter", Input).has_focus
        assert listing.highlighted_child.data_value == "deepseek deepseek-chat"
        await pilot.press("down")  # over the "openai" header
        assert listing.highlighted_child.data_value == "openai gpt-0"

        await pilot.press(*"gpt-1")
        await pilot.pause()
        values = [getattr(item, "data_value", None) for item in listing.children]
        # header kept for its group, other groups gone
        assert values == [None, "openai gpt-1", "openai gpt-10", "openai gpt-11"]
        await pilot.press("down", "enter")
        await pilot.pause()
    assert picked == ["openai gpt-10"]


async def test_choice_modal_filter_with_no_match_shows_it_and_enter_does_nothing():
    from luna.tui.pickers import ChoiceModal
    from tests.tui_fakes import make_app

    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        modal = ChoiceModal("Модель", _model_options())
        app.push_screen(modal)
        await pilot.pause()
        await pilot.press(*"zzz", "enter")
        await pilot.pause()
        assert app.screen is modal
        assert modal.query_one("#picker-empty").display


async def test_short_choice_list_has_no_filter():
    from luna.tui.pickers import ChoiceModal
    from tests.tui_fakes import make_app

    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        app.push_screen(ChoiceModal("Провайдер", [("a", "a"), ("b", "b")]))
        await pilot.pause()
        assert not app.screen.query("#picker-filter")


# -- REPL picker --------------------------------------------------------------


def test_arrow_pick_turns_headers_into_separators_and_filters_long_lists(monkeypatch):
    import questionary

    from luna.ui import interact

    monkeypatch.setattr(interact, "_real_terminal", lambda console, input_fn: True)
    seen = {}

    class _Q:
        def ask(self):
            return "openai gpt-1"

    def fake_select(message, choices, **kwargs):
        seen.update(kwargs, choices=choices)
        return _Q()

    monkeypatch.setattr("questionary.select", fake_select)
    picked = interact.arrow_pick(None, input, _model_options())
    assert picked == "openai gpt-1"
    assert isinstance(seen["choices"][0], questionary.Separator)
    assert seen["use_search_filter"] is True and seen["use_jk_keys"] is False


async def test_esc_in_the_filter_cancels_the_picker():
    from luna.tui.pickers import ChoiceModal
    from tests.tui_fakes import make_app

    app = make_app(theme_choice="moonlight")
    picked = []
    async with app.run_test() as pilot:
        app.push_screen(ChoiceModal("Модель", _model_options()), picked.append)
        await pilot.pause()
        await pilot.press(*"gpt", "escape")
        await pilot.pause()
        assert not isinstance(app.screen, ChoiceModal)
    assert picked == [None]
