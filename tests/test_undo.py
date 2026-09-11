import json
import os
import subprocess
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


def _git(tmp_path, *args):
    subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)


def test_begin_turn_creates_a_shadow_ref(tmp_path):
    from luna.undo import begin_turn

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    begin_turn(str(tmp_path), "sess1", message_count=1)

    out = subprocess.run(
        ["git", "rev-parse", "refs/luna/undo/sess1"], cwd=tmp_path, capture_output=True, text=True
    )
    assert out.returncode == 0 and out.stdout.strip()
    ledger = tmp_path / ".luna" / "undo" / "sess1" / "turns.json"
    assert ledger.is_file()


def test_begin_turn_is_a_noop_outside_git(tmp_path):
    from luna.undo import begin_turn

    begin_turn(str(tmp_path), "sess1", message_count=1)  # must not raise
    assert not (tmp_path / ".luna" / "undo").exists()


def test_begin_turn_handles_an_unborn_head(tmp_path):
    from luna.undo import begin_turn

    _git(tmp_path, "init", "-q")  # no commits yet
    begin_turn(str(tmp_path), "sess1", message_count=0)  # must not raise
    ledger = tmp_path / ".luna" / "undo" / "sess1" / "turns.json"
    assert ledger.is_file()


def test_begin_turn_does_not_touch_the_users_index_or_worktree(tmp_path):
    from luna.undo import begin_turn

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    (tmp_path / "a.txt").write_text("staged-change\n")
    _git(tmp_path, "add", "a.txt")  # user has a staged change

    begin_turn(str(tmp_path), "sess1", message_count=1)

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "M  a.txt" in status  # still staged, untouched by the snapshot
    assert (tmp_path / "a.txt").read_text() == "staged-change\n"  # worktree untouched


def test_undo_restores_files_and_truncates_the_conversation(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": "1",
                    "args": {"file_path": "/a.txt", "content": "v1\n"},
                }
            ],
        ),
        AIMessage(content="changed a.txt"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}

    pre = agent.get_state(cfg).values.get("messages", [])
    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "change it"}]}, config=cfg)
    assert (tmp_path / "a.txt").read_text() == "v1\n"
    after = len(agent.get_state(cfg).values["messages"])
    assert after > len(pre)

    note = undo(str(tmp_path), "sess1", agent, thread_id)
    assert note is not None
    assert (tmp_path / "a.txt").read_text() == "v0\n"
    assert len(agent.get_state(cfg).values["messages"]) == len(pre)


def test_undo_with_no_turns_returns_none(tmp_path):
    from luna.undo import undo

    _git(tmp_path, "init", "-q")
    assert undo(str(tmp_path), "sess-empty", agent=None, thread_id="t") is None


def test_redo_restores_files_and_messages(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, redo, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": "1",
                    "args": {"file_path": "/a.txt", "content": "v1\n"},
                }
            ],
        ),
        AIMessage(content="changed a.txt"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}

    pre = agent.get_state(cfg).values.get("messages", [])
    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "change it"}]}, config=cfg)
    after_count = len(agent.get_state(cfg).values["messages"])

    undo(str(tmp_path), "sess1", agent, thread_id)
    redo(str(tmp_path), "sess1", agent, thread_id)

    assert (tmp_path / "a.txt").read_text() == "v1\n"
    assert len(agent.get_state(cfg).values["messages"]) == after_count


def test_redo_with_nothing_to_redo_returns_none(tmp_path):
    from luna.undo import redo

    _git(tmp_path, "init", "-q")
    assert redo(str(tmp_path), "sess-empty", agent=None, thread_id="t") is None


def test_a_new_turn_clears_the_redo_stack(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, redo, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(AIMessage(content="ok"))
    )
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}
    pre = agent.get_state(cfg).values.get("messages", [])

    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "turn 1"}]}, config=cfg)
    undo(str(tmp_path), "sess1", agent, thread_id)

    begin_turn(
        str(tmp_path), "sess1", len(agent.get_state(cfg).values["messages"])
    )  # a fresh turn begins

    assert redo(str(tmp_path), "sess1", agent, thread_id) is None


def test_undo_redo_undo_reverts_files_the_second_time(tmp_path, fake_model):
    """Regression: redo() must preserve the original pre_sha for a later undo()."""
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, redo, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": "1",
                    "args": {"file_path": "/a.txt", "content": "v1\n"},
                }
            ],
        ),
        AIMessage(content="changed a.txt"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}

    pre = agent.get_state(cfg).values.get("messages", [])
    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "change it"}]}, config=cfg)
    assert (tmp_path / "a.txt").read_text() == "v1\n"

    undo(str(tmp_path), "sess1", agent, thread_id)
    assert (tmp_path / "a.txt").read_text() == "v0\n"

    redo(str(tmp_path), "sess1", agent, thread_id)
    assert (tmp_path / "a.txt").read_text() == "v1\n"

    undo(str(tmp_path), "sess1", agent, thread_id)
    assert (tmp_path / "a.txt").read_text() == "v0\n"  # second undo must revert again


def test_undo_deletes_a_file_the_turn_created(tmp_path, fake_model):
    """Regression: `git checkout` never deletes a path absent from pre_sha's tree."""
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": "1",
                    "args": {"file_path": "/new.txt", "content": "hi\n"},
                }
            ],
        ),
        AIMessage(content="created new.txt"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}

    pre = agent.get_state(cfg).values.get("messages", [])
    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "make new.txt"}]}, config=cfg)
    assert (tmp_path / "new.txt").read_text() == "hi\n"

    undo(str(tmp_path), "sess1", agent, thread_id)
    assert not (tmp_path / "new.txt").exists()


def test_redo_deletes_a_file_the_turn_deleted(tmp_path, fake_model):
    """Regression: the mirror case — redo() re-applying a deletion `git checkout` can't undo."""
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, redo, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    calls = [
        AIMessage(
            content="",
            tool_calls=[{"name": "delete", "id": "1", "args": {"file_path": "/a.txt"}}],
        ),
        AIMessage(content="deleted a.txt"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}

    pre = agent.get_state(cfg).values.get("messages", [])
    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "delete a.txt"}]}, config=cfg)
    assert not (tmp_path / "a.txt").exists()

    undo(str(tmp_path), "sess1", agent, thread_id)
    assert (tmp_path / "a.txt").read_text() == "v0\n"  # deletion undone, file restored

    redo(str(tmp_path), "sess1", agent, thread_id)
    assert not (tmp_path / "a.txt").exists()  # deletion re-applied
