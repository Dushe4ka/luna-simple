import sqlite3

from luna_core.session import EventLog

from luna.config.config import config_dir
from luna.core import store


def test_db_path_is_in_the_config_dir():
    path = store.db_path()
    assert path == config_dir() / "sessions.db" and path.parent.is_dir()


def test_open_log_is_one_event_log_per_process():
    log = store.open_log()
    assert isinstance(log, EventLog) and store.open_log() is log
    sid = log.create("/repo", title="t")
    assert store.open_log().info(sid).title == "t"


def test_open_log_drops_the_legacy_langgraph_tables_once():
    with sqlite3.connect(store.db_path()) as conn:
        for table in ("checkpoints", "writes", "luna_sessions", "luna_session_state"):
            conn.execute(f"CREATE TABLE {table} (x)")
    store.open_log()
    with sqlite3.connect(store.db_path()) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not names & {"checkpoints", "writes", "luna_sessions"}
    assert "luna_session_state" in names  # per-session settings are kept
    assert "sessions" in names  # the event log's own tables


def test_make_title_collapses_and_truncates():
    assert store.make_title("  hello   world\n") == "hello world"
    assert len(store.make_title("x" * 100)) == 73 and store.make_title("x" * 100).endswith("…")


def test_conversations_skip_children_and_empty_sessions():
    from luna_core.messages import Message, message_to_dict

    log = store.open_log()
    real = log.create("/repo", title="real")
    log.append(real, "user", {"message": message_to_dict(Message.user("hi"))})
    log.create("/repo", title="empty")
    child = log.create("/repo", title="child", parent_id=real)
    log.append(child, "user", {"message": message_to_dict(Message.user("sub"))})
    assert [i.id for i in store.conversations(log, "/repo")] == [real]
