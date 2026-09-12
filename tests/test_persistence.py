import time

from langchain_core.messages import AIMessage
from rich.console import Console

from luna.config.config import LunaConfig, config_dir
from luna.core.agent import build_agent
from luna.core.persistence import SessionIndex, checkpointer, make_title
from luna.core.session import run_once


def test_make_title_collapses_and_truncates():
    assert make_title("  hello   world\n") == "hello world"
    assert make_title("x" * 100).endswith("…")
    assert len(make_title("x" * 100)) == 73


def test_index_record_and_latest_for(tmp_path):
    idx = SessionIndex()
    idx.record("t1", "/repo/a", "first")
    time.sleep(0.01)
    idx.record("t2", "/repo/a", "second")
    idx.record("t3", "/repo/b", "other")
    assert idx.latest_for("/repo/a").thread_id == "t2"
    assert idx.latest_for("/repo/b").thread_id == "t3"
    assert idx.latest_for("/repo/missing") is None


def test_index_touch_changes_order(tmp_path):
    idx = SessionIndex()
    idx.record("t1", "/r", "one")
    idx.record("t2", "/r", "two")
    time.sleep(0.01)
    idx.touch("t1")
    assert [r.thread_id for r in idx.list("/r")] == ["t1", "t2"]


def test_session_index_survives_unwritable_db(tmp_path, monkeypatch):
    # point the config dir at a path whose parent is a FILE -> mkdir/connect fail
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker / "config"))
    idx = SessionIndex()
    assert idx.ok is False
    idx.record("t", ".", "title")  # no raise
    idx.touch("t")  # no raise
    assert idx.latest_for(".") is None
    assert idx.list(".") == []


def test_checkpointer_falls_back_to_memory(tmp_path, monkeypatch, fake_model):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker / "config"))
    warned: list[str] = []
    cp = checkpointer(on_warn=warned.append)
    assert hasattr(cp, "get") and hasattr(cp, "put")
    assert len(warned) == 1 and "sessions.db" in warned[0]
    # a real agent still works with the fallback saver
    from luna.config.config import LunaConfig
    from luna.core.agent import build_agent

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "good"))
    build_agent(LunaConfig(workdir=str(tmp_path)), model=fake_model(), checkpointer=cp)


def _db_file(saver):
    (row,) = saver.conn.execute("PRAGMA database_list").fetchall()
    return row[2]  # the resolved on-disk path of the "main" database


def test_history_survives_rebuild(tmp_path, fake_model):
    cp = checkpointer()
    idx = SessionIndex()
    cfg = LunaConfig(workdir=str(tmp_path))
    a1 = build_agent(cfg, model=fake_model(AIMessage(content="one")), checkpointer=cp)
    run_once(
        a1,
        "remember X",
        thread_id="keep",
        console=Console(),
        index=idx,
        workdir=str(tmp_path),
    )
    a2 = build_agent(cfg, model=fake_model(AIMessage(content="two")), checkpointer=cp)
    state = a2.get_state({"configurable": {"thread_id": "keep"}})
    assert any("remember X" in getattr(m, "content", "") for m in state.values["messages"])


def test_checkpointer_shares_one_on_disk_db():
    cp1 = checkpointer()
    assert hasattr(cp1, "get") and hasattr(cp1, "put")

    db = config_dir() / "sessions.db"
    assert db.exists()

    cp2 = checkpointer()
    # A miss returns None rather than raising.
    assert cp2.get({"configurable": {"thread_id": "nope", "checkpoint_ns": ""}}) is None

    # Both instances are bound to the very same file on disk.
    assert _db_file(cp1) == _db_file(cp2) == str(db)
