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


def test_snapshot_numbering_is_monotonic(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v0\n")
    snapshot(str(tmp_path), "s2", "edit_file", "a.py")
    f.write_text("v1\n")
    snapshot(str(tmp_path), "s2", "edit_file", "a.py")
    names = sorted(p.name for p in journal_dir(str(tmp_path), "s2").glob("[0-9]*.json"))
    assert names == ["0000.json", "0001.json"]


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
    assert (tmp_path / ".luna" / "undo" / "sid" / "0000.json").exists()


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
