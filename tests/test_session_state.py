import sqlite3

from luna.core.session_state import SessionState, SessionStateStore
from luna.core.store import db_path


def test_unknown_session_loads_defaults(tmp_path):
    state = SessionStateStore().load("t1", str(tmp_path))
    assert state == SessionState("t1", str(tmp_path))
    assert state.plan is False and state.pinned.paths == []
    assert state.dirty_before == [] and state.mutated is False and state.fixup_turn is None
    assert not hasattr(state, "usage")


def test_every_field_round_trips(tmp_path):
    store = SessionStateStore()
    state = store.load("t1", str(tmp_path))
    state.provider, state.model, state.plan = "openai", "gpt-5", True
    state.pinned.add("a.py", "b.py")
    state.pending_diagnostics = "a.py:1 E501"
    state.mutated, state.fixup_turn, state.dirty_before = True, "turn", ["x"]
    store.save(state)

    again = SessionStateStore().load("t1", str(tmp_path))
    assert (again.provider, again.model, again.plan) == ("openai", "gpt-5", True)
    assert again.pinned.paths == ["a.py", "b.py"]
    assert again.pending_diagnostics == "a.py:1 E501"
    # the pipeline's per-turn bookkeeping is in-memory only
    assert (again.mutated, again.fixup_turn, again.dirty_before) == (False, None, [])


def test_an_old_table_with_a_usage_column_still_works(tmp_path):
    with sqlite3.connect(db_path()) as conn:
        conn.execute(
            "CREATE TABLE luna_session_state (thread_id TEXT PRIMARY KEY, workdir TEXT NOT NULL, "
            "provider TEXT, model TEXT, plan INTEGER NOT NULL DEFAULT 0, "
            "pinned TEXT NOT NULL DEFAULT '[]', pending_diagnostics TEXT NOT NULL DEFAULT '', "
            "usage TEXT NOT NULL DEFAULT '[]')"
        )
    store = SessionStateStore()
    state = store.load("t1", str(tmp_path))
    state.plan = True
    store.save(state)
    store.save(state)  # the upsert path too
    assert SessionStateStore().load("t1", str(tmp_path)).plan is True


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
