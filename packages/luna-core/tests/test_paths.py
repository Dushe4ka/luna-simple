from pathlib import Path

from luna_core.paths import user_config_dir


def test_xdg_wins_everywhere():
    assert user_config_dir({"XDG_CONFIG_HOME": "/x"}, windows=True) == Path("/x") / "luna"


def test_windows_uses_appdata():
    env = {"APPDATA": r"C:\Users\u\AppData\Roaming"}
    assert user_config_dir(env, windows=True) == Path(env["APPDATA"]) / "luna"


def test_posix_default(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert user_config_dir({}, windows=False) == tmp_path / ".config" / "luna"
