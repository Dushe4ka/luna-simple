import time

from luna.config import config_dir
from luna.persistence import SessionIndex, checkpointer, make_title


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


def _db_file(saver):
    (row,) = saver.conn.execute("PRAGMA database_list").fetchall()
    return row[2]  # the resolved on-disk path of the "main" database


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
