import pytest

from luna.ui import themes
from luna.ui.themes import BUILTIN_ORDER, TOKENS, available_themes, contrast, load_theme

READABLE = ("text", "muted", "strong", "primary", "tool", "code", "ok", "warn", "err", "add", "del")


def test_builtin_order_and_titles():
    got = [t.id for t in available_themes()]
    assert got[: len(BUILTIN_ORDER)] == list(BUILTIN_ORDER)
    assert load_theme("moonlight").title == "Лунный свет"
    assert load_theme("daymoon").title == "Дневная луна"
    assert load_theme("daymoon").dark is False
    assert load_theme("ansi-dark").ansi is True


@pytest.mark.parametrize("theme_id", BUILTIN_ORDER)
def test_builtin_defines_every_token(theme_id):
    assert set(load_theme(theme_id).tokens) == set(TOKENS)


@pytest.mark.parametrize("theme_id", [t for t in BUILTIN_ORDER if not t.startswith("ansi")])
def test_builtin_contrast(theme_id):
    tk = load_theme(theme_id).tokens
    for name in READABLE:
        for surface in ("bg", "panel", "element"):
            assert contrast(tk[name], tk[surface]) >= 4.5, (theme_id, name, surface)
    for surface in ("add_bg", "del_bg"):
        assert contrast(tk["text"], tk[surface]) >= 4.5
    assert contrast(tk["on_primary"], tk["primary"]) >= 4.5


def test_exact_spec_values():
    assert load_theme("moonlight").tokens["primary"] == "#a3b5ff"
    assert load_theme("daymoon").tokens["bg"] == "#f8f7f3"
    assert load_theme("moonlight-cb").tokens["ok"] == "#6cb8ee"
    assert load_theme("daymoon-cb").tokens["err"] == "#9a4806"


def _user_theme(tmp_path, name, body):
    d = tmp_path / ".config" / "luna" / "themes"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(body, encoding="utf-8")


def test_user_theme_extends(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _user_theme(
        tmp_path,
        "mine",
        'title = "Моя"\nextends = "moonlight"\n[tokens]\nprimary = "#ffaa00"\n',
    )
    spec = load_theme("mine")
    assert spec.tokens["primary"] == "#ffaa00"
    assert spec.tokens["bg"] == load_theme("moonlight").tokens["bg"]
    assert spec.dark is True


def test_broken_user_theme_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _user_theme(tmp_path, "bad", "title = [unclosed")
    ids = [t.id for t in available_themes()]
    assert "bad" not in ids
    assert any("bad" in w for w in themes.theme_warnings())


def test_user_theme_cannot_shadow_builtin(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _user_theme(tmp_path, "moonlight", 'title = "X"\nextends = "daymoon"\n')
    assert load_theme("moonlight").title == "Лунный свет"
    assert any("moonlight" in w for w in themes.theme_warnings())


def test_unknown_token_skips_theme(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _user_theme(
        tmp_path,
        "odd",
        'title = "Odd"\nextends = "moonlight"\n[tokens]\nsparkle = "#fff000"\n',
    )
    assert "odd" not in [t.id for t in available_themes()]


def test_low_contrast_user_theme_is_lifted(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _user_theme(
        tmp_path,
        "dim",
        'title = "Dim"\nextends = "moonlight"\n[tokens]\nmuted = "#20242c"\n',
    )
    spec = load_theme("dim")
    assert contrast(spec.tokens["muted"], spec.tokens["element"]) >= 4.5
    assert any("muted" in w for w in themes.theme_warnings())


def test_missing_theme_raises():
    with pytest.raises(KeyError):
        load_theme("nope")
