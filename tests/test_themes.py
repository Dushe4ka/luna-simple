import pytest
from rich.style import Style

from luna.ui import themes
from luna.ui.themes import (
    BUILTIN_ORDER,
    TOKENS,
    active,
    available_themes,
    contrast,
    load_theme,
    resolve,
    rich_theme,
    set_active,
    textual_theme,
    theme_warnings,
)

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


@pytest.mark.parametrize(
    "body",
    [
        '[tokens]\nprimary = "red"\n',
        '[tokens]\nprimary = "#fff"\n',
        '[tokens]\nprimary = "#12"\n',
        "tokens = 5\n",
        "[tokens]\nprimary = 5\n",
        "extends = [1]\n",
        "extends = 7\n",
        'extends = "ghost"\n',
        '[tokens]\nbg = "#000000"\n',
    ],
)
def test_bad_user_theme_is_skipped_with_warning(monkeypatch, tmp_path, body):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    if "extends" not in body or body.startswith("[tokens]"):
        body = (
            'extends = "moonlight"\n' + body
            if body.startswith("[tokens]") and "bg" not in body
            else body
        )
    _user_theme(tmp_path, "bad", body)
    ids = [t.id for t in available_themes()]
    assert "bad" not in ids
    assert ids[: len(BUILTIN_ORDER)] == list(BUILTIN_ORDER)
    assert any("bad" in w for w in themes.theme_warnings())


def test_unreadable_user_dir_keeps_builtins(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _user_theme(tmp_path, "x", 'extends = "moonlight"\n')

    def boom(self, pattern):
        raise PermissionError("denied")

    monkeypatch.setattr("pathlib.Path.glob", boom)
    assert [t.id for t in available_themes()] == list(BUILTIN_ORDER)
    assert themes.theme_warnings()


def test_resolve_auto():
    assert resolve("auto", dark_terminal=True).id == "moonlight"
    assert resolve("auto", dark_terminal=False).id == "daymoon"
    assert resolve("daymoon-cb", dark_terminal=True).id == "daymoon-cb"


def test_resolve_unknown_falls_back_to_auto():
    assert resolve("neon", dark_terminal=False).id == "daymoon"
    assert any("neon" in w for w in theme_warnings())


def test_rich_theme_styles():
    spec = load_theme("moonlight")
    tokens = spec.tokens
    styles = rich_theme(spec).styles
    assert styles["luna.primary"] == Style.parse(tokens["primary"])
    assert styles["luna.error"] == Style.parse(tokens["err"])
    assert styles["luna.diff.add"] == Style.parse(f"{tokens['add']} on {tokens['add_bg']}")
    assert styles["luna.border_subtle"] == Style.parse(tokens["border_subtle"])
    assert styles["luna.title"] == Style.parse(f"bold {tokens['strong']}")
    assert styles["luna.primary"] == Style.parse("#a3b5ff")


def test_rich_theme_ansi_names():
    styles = rich_theme(load_theme("ansi-dark")).styles
    assert styles["luna.primary"] == Style.parse("bright_blue")


def test_textual_theme_variables():
    t = textual_theme(load_theme("daymoon"))
    assert t.name == "luna-daymoon" and t.dark is False
    assert t.background == "#f8f7f3" and t.foreground == "#1e2026"
    assert t.variables["muted"] == "#5f636d"
    assert t.variables["add-bg"] == "#dcefe0"
    assert "text" not in t.variables


def test_textual_theme_ansi():
    t = textual_theme(load_theme("ansi-light"))
    assert t.ansi is True and t.variables["primary"] == "ansi_blue"
    assert t.background == "ansi_default"


@pytest.mark.parametrize("theme_id", ["ansi-dark", "ansi-light"])
def test_textual_ansi_theme_keeps_textuals_ansi_variables(theme_id):
    # Textual's own CSS for ANSI screens references $ansi-background etc.:
    # without them a TUI on an ANSI theme fails to parse its stylesheet.
    t = textual_theme(load_theme(theme_id))
    assert "ansi-background" in t.variables and "ansi-foreground" in t.variables
    assert t.variables["primary"].startswith("ansi_")


def test_active_roundtrip():
    before = active()
    try:
        set_active(load_theme("daymoon"))
        assert active().id == "daymoon"
    finally:
        set_active(before)
