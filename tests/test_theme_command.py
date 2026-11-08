"""``/theme``: show, pick, set and save the color theme (shared command)."""

from dataclasses import dataclass, field

from luna.commands import REGISTRY, run_line
from luna.config.config import LunaConfig, user_setting


@dataclass
class _Env:
    cfg: LunaConfig = field(default_factory=LunaConfig)
    user_commands: dict = field(default_factory=dict)
    workdir: str = "."

    @property
    def config(self):
        return self.cfg


async def test_theme_is_registered_and_listed_in_help():
    from luna.repl.commands import HELP

    assert "/theme" in REGISTRY
    assert "/theme" in HELP


async def test_theme_without_arg_asks():
    result = await run_line("/theme", _Env())
    assert result.effects["pick_theme"]["current"] == "auto"
    assert result.notices[0].level == "dim"
    assert "auto" in result.notices[0].text


async def test_theme_sets_and_saves():
    env = _Env()
    result = await run_line("/theme daymoon", env)
    assert result.effects == {"theme": "daymoon"}
    assert user_setting("ui.theme") == "daymoon"
    assert env.config.theme == "daymoon"
    assert result.notices[0].level == "info"
    assert "Дневная луна" in result.notices[0].text


async def test_theme_auto_is_accepted():
    result = await run_line("/theme auto", _Env(cfg=LunaConfig(theme="daymoon")))
    assert result.effects == {"theme": "auto"}
    assert user_setting("ui.theme") == "auto"


async def test_theme_unknown():
    env = _Env()
    result = await run_line("/theme neon", env)
    notice = result.notices[0]
    assert notice.level == "error"
    assert "'neon'" in notice.text
    assert "auto, moonlight, daymoon" in notice.text
    assert result.effects == {}
    assert user_setting("ui.theme") is None
    assert env.config.theme == "auto"


async def test_theme_save_failure_still_applies_for_the_session(monkeypatch):
    def boom(updates, **kw):
        raise OSError("read-only")

    monkeypatch.setattr("luna.config.config.set_config_values", boom)
    env = _Env()
    result = await run_line("/theme daymoon", env)
    assert result.effects == {"theme": "daymoon"}
    assert env.config.theme == "daymoon"
    assert any(n.level == "error" and "не удалось сохранить" in n.text for n in result.notices)
