import json
import os
import time

from langchain_core.messages import AIMessage

from luna.agent import build_agent
from luna.config import LunaConfig
from luna.undo import journal_dir, peek_last, session_diff, snapshot, undo_last


def test_snapshot_and_undo_modify(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("old\n")
    snapshot(str(tmp_path), "s1", "edit_file", "a.py")
    f.write_text("new\n")
    assert "old" in session_diff(str(tmp_path), "s1") and "new" in session_diff(str(tmp_path), "s1")
    assert undo_last(str(tmp_path), "s1") is not None
    assert f.read_text() == "old\n"


def test_snapshot_and_undo_create(tmp_path):
    snapshot(str(tmp_path), "s1", "write_file", "new.py")
    (tmp_path / "new.py").write_text("created\n")
    undo_last(str(tmp_path), "s1")
    assert not (tmp_path / "new.py").exists()


def test_undo_empty_returns_none(tmp_path):
    assert undo_last(str(tmp_path), "empty") is None


def test_peek_last_describes_revert_without_mutating(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("old\n")
    snapshot(str(tmp_path), "p", "edit_file", "a.py")
    f.write_text("new\n")
    assert peek_last(str(tmp_path), "p") == "revert a.py"
    assert f.read_text() == "new\n"  # peek did not touch the file
    assert list(journal_dir(str(tmp_path), "p").glob("[0-9]*.json"))  # nor the journal


def test_peek_last_describes_delete_and_empty(tmp_path):
    assert peek_last(str(tmp_path), "none") is None
    snapshot(str(tmp_path), "c", "write_file", "created.py")
    assert peek_last(str(tmp_path), "c") == "delete created.py (was newly created)"


def test_snapshot_entries_sort_chronologically(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v0\n")
    snapshot(str(tmp_path), "s2", "edit_file", "a.py")
    f.write_text("v1\n")
    snapshot(str(tmp_path), "s2", "edit_file", "a.py")
    entries = sorted(journal_dir(str(tmp_path), "s2").glob("[0-9]*.json"))
    assert len(entries) == 2  # two distinct entries, no collision
    befores = [json.loads(e.read_text())["before"] for e in entries]
    assert befores == ["v0\n", "v1\n"]  # sorted order == insertion order


def test_parallel_snapshots_do_not_collide(tmp_path):
    import threading

    f = tmp_path / "a.py"
    f.write_text("v0\n")
    barrier = threading.Barrier(2)

    def take():
        barrier.wait()
        snapshot(str(tmp_path), "par", "edit_file", "a.py")

    threads = [threading.Thread(target=take) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    entries = list(journal_dir(str(tmp_path), "par").glob("[0-9]*.json"))
    assert len(entries) == 2  # both pre-images survived


def test_undo_walks_back_through_history(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v0\n")
    snapshot(str(tmp_path), "s3", "edit_file", "a.py")
    f.write_text("v1\n")
    snapshot(str(tmp_path), "s3", "edit_file", "a.py")
    f.write_text("v2\n")
    undo_last(str(tmp_path), "s3")
    assert f.read_text() == "v1\n"
    undo_last(str(tmp_path), "s3")
    assert f.read_text() == "v0\n"


def test_missing_path_never_raises(tmp_path):
    assert session_diff(str(tmp_path / "nope"), "x") == ""
    assert undo_last(str(tmp_path / "nope"), "x") is None


def test_non_utf8_file_never_raises(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe\x00")
    snapshot(str(tmp_path), "bin", "write_file", "b.bin")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02")
    # neither call raises UnicodeDecodeError on the binary content
    assert isinstance(session_diff(str(tmp_path), "bin"), str)
    assert undo_last(str(tmp_path), "bin") is not None


def test_empty_json_entry_is_skipped(tmp_path):
    journal_dir(str(tmp_path), "e").joinpath("0000.json").write_text("{}")
    assert session_diff(str(tmp_path), "e") == ""
    assert undo_last(str(tmp_path), "e") is None


def test_snapshot_lands_via_middleware(tmp_path, fake_model):
    write_call = AIMessage(
        content="",
        tool_calls=[
            {"name": "write_file", "id": "1", "args": {"file_path": "/hi.txt", "content": "hi\n"}}
        ],
    )
    done = AIMessage(content="done")
    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True),
        model=fake_model(write_call, done),
        session_id="sid",
    )
    agent.invoke(
        {"messages": [{"role": "user", "content": "make hi.txt"}]},
        config={"configurable": {"thread_id": "t1"}},
    )
    assert list((tmp_path / ".luna" / "undo" / "sid").glob("[0-9]*.json"))


def test_denied_write_makes_no_journal_entry(tmp_path, fake_model):
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text('deny = ["write_file:*"]\n')
    write_call = AIMessage(
        content="",
        tool_calls=[
            {"name": "write_file", "id": "1", "args": {"file_path": "/x.txt", "content": "x\n"}}
        ],
    )
    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True),
        model=fake_model(write_call, AIMessage(content="done")),
        session_id="d",
    )
    agent.invoke(
        {"messages": [{"role": "user", "content": "write x.txt"}]},
        config={"configurable": {"thread_id": "t1"}},
    )
    journal = tmp_path / ".luna" / "undo" / "d"
    assert not journal.exists() or not list(journal.glob("*.json"))


def test_undo_refuses_to_delete_a_changed_binary(tmp_path):
    from luna.undo import peek_last, snapshot, undo_last

    f = tmp_path / "logo.bin"
    f.write_bytes(b"\x89PNG\x00original")
    snapshot(str(tmp_path), "s", "write_file", "logo.bin")  # before=None, existed=True
    f.write_bytes(b"\x89PNG\x00changed")
    assert "binary" in (peek_last(str(tmp_path), "s") or "").lower()
    note = undo_last(str(tmp_path), "s")
    assert f.exists() and f.read_bytes() == b"\x89PNG\x00changed"
    assert "cannot revert" in note


def test_undo_still_deletes_a_created_file(tmp_path):
    from luna.undo import snapshot, undo_last

    snapshot(str(tmp_path), "s", "write_file", "new.py")  # existed=False
    (tmp_path / "new.py").write_text("x\n")
    undo_last(str(tmp_path), "s")
    assert not (tmp_path / "new.py").exists()


def test_read_paths_do_not_create_the_journal_dir(tmp_path):
    from luna.undo import session_diff, undo_last

    assert session_diff(str(tmp_path), "none") == ""
    assert undo_last(str(tmp_path), "none") is None
    assert not (tmp_path / ".luna" / "undo" / "none").exists()


def test_session_diff_marks_binary_entries(tmp_path):
    from luna.undo import session_diff, snapshot

    (tmp_path / "b.bin").write_bytes(b"\xff\x00\xfe")
    snapshot(str(tmp_path), "s", "edit_file", "b.bin")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    assert "binary or unreadable" in session_diff(str(tmp_path), "s")


def test_gc_removes_old_journals(tmp_path):
    from luna.undo import gc, journal_dir

    old = journal_dir(str(tmp_path), "old")
    (old / "0000.json").write_text("{}")
    fresh = journal_dir(str(tmp_path), "fresh")
    (fresh / "0000.json").write_text("{}")
    past = time.time() - 40 * 86400
    os.utime(old / "0000.json", (past, past))
    os.utime(old, (past, past))
    gc(str(tmp_path), keep_days=7)
    assert not old.exists() and fresh.exists()


def test_gc_never_removes_the_kept_session(tmp_path):
    from luna.undo import gc, journal_dir

    keepme = journal_dir(str(tmp_path), "keepme")
    (keepme / "0000.json").write_text("{}")
    past = time.time() - 40 * 86400
    os.utime(keepme / "0000.json", (past, past))
    os.utime(keepme, (past, past))
    gc(str(tmp_path), keep_days=7, keep="keepme")
    assert keepme.exists()  # resumed session's journal survives even when stale
