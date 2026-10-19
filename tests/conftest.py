import pytest


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
