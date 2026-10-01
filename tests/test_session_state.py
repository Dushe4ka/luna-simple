from luna.config.usage import TurnUsage
from luna.core.session_state import SessionState, SessionStateStore


def test_unknown_session_loads_defaults(tmp_path):
    state = SessionStateStore().load("t1", str(tmp_path))
    assert state == SessionState("t1", str(tmp_path))
    assert state.plan is False and state.pinned.paths == [] and state.usage.turns == []


def test_every_field_round_trips(tmp_path):
    store = SessionStateStore()
    state = store.load("t1", str(tmp_path))
    state.provider, state.model, state.plan = "openai", "gpt-5", True
    state.pinned.add("a.py", "b.py")
    state.pending_diagnostics = "a.py:1 E501"
    state.usage.add_turn(TurnUsage(10, 5, 15))
    store.save(state)

    again = SessionStateStore().load("t1", str(tmp_path))
    assert (again.provider, again.model, again.plan) == ("openai", "gpt-5", True)
    assert again.pinned.paths == ["a.py", "b.py"]
    assert again.pending_diagnostics == "a.py:1 E501"
    assert again.usage.totals == (10, 5, 15)


def test_sessions_do_not_share_state(tmp_path):
    store = SessionStateStore()
    one = store.load("t1", str(tmp_path))
    one.plan = True
    store.save(one)
    assert store.load("t2", str(tmp_path)).plan is False


def test_degrades_to_defaults_when_the_db_cannot_open(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker / "cfg"))
    store = SessionStateStore()
    assert store.ok is False
    state = store.load("t1", str(tmp_path))
    state.plan = True
    store.save(state)  # no-op, no raise
    assert store.load("t1", str(tmp_path)).plan is False
