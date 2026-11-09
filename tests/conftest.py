import pytest


@pytest.fixture(autouse=True)
def no_real_terminal(request, monkeypatch):
    """Never query the real terminal or open the theme picker on real stdin."""
    from luna.ui import termbg, theme_picker

    termbg.reset_cache()
    if not request.module.__name__.endswith("test_termbg"):
        monkeypatch.setattr(termbg, "query_osc11", lambda *a, **k: None)

    def refuse(**kw):
        raise AssertionError("theme picker opened in a test without a stub")

    monkeypatch.setattr(theme_picker, "pick_theme", refuse)
    yield
    termbg.reset_cache()


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path_factory, monkeypatch):
    """Keep tests away from the real ~/.config/luna (env var and HOME)."""
    from luna.core import store

    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    # each test has its own config home, so its own sessions.db and event log
    for log in store._LOGS.values():
        log.close()
    store._LOGS.clear()
    return home / ".config"


@pytest.fixture
def fake_provider():
    """Build a scripted provider: fake_provider(FakeProvider.text("hi"), FakeProvider.call(...))."""
    from luna_core.providers.fake import FakeProvider

    def _make(*script):
        return FakeProvider(list(script) or [FakeProvider.text("ok")])

    return _make
