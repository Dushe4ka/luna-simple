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


def test_snapshot_tree_returns_none_on_a_genuine_add_failure_not_an_empty_tree(tmp_path):
    """A GENUINE `git add` failure (e.g. an unreadable file) must not produce an
    EMPTY tree that a later undo() would read as "every file was added by this
    turn" and delete. Unlike the ignored-paths warning (which still writes a
    correct temp index despite a nonzero exit), a real failure never writes the
    index at all — that's the signal _snapshot_tree uses to tell them apart."""
    import stat

    from luna.undo import _snapshot_tree

    _git(tmp_path, "init", "-q")
    (tmp_path / "keep.txt").write_text("keep\n")
    (tmp_path / "locked.txt").write_text("locked\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    locked = tmp_path / "locked.txt"
    locked.chmod(0o000)
    try:
        tree = _snapshot_tree(str(tmp_path))
    finally:
        locked.chmod(stat.S_IRUSR | stat.S_IWUSR)  # restore so tmp_path cleanup works

    assert tree is None


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


def test_session_diff_uses_git_when_available(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, session_diff

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
    cfg = {"configurable": {"thread_id": "t"}}
    pre = agent.get_state(cfg).values.get("messages", [])
    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "change it"}]}, config=cfg)

    diff = session_diff(str(tmp_path), "sess1")
    assert "v0" in diff and "v1" in diff


def test_undo_redo_undo_leaves_the_thread_usable(tmp_path, fake_model):
    """The message-side mirror of test_undo_redo_undo_reverts_files_the_second_time:
    after undo -> redo -> undo, the thread's message list must be genuinely restorable
    (no leftover un-removable RemoveMessage), and a NEXT turn against the agent must
    actually succeed rather than raising "Unknown BaseMessage type"."""
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
        AIMessage(content="ok, second turn worked"),  # the turn AFTER the second undo
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}
    pre = agent.get_state(cfg).values.get("messages", [])

    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "change it"}]}, config=cfg)

    undo(str(tmp_path), "sess1", agent, thread_id)
    redo(str(tmp_path), "sess1", agent, thread_id)
    undo(str(tmp_path), "sess1", agent, thread_id)

    messages_after = agent.get_state(cfg).values["messages"]
    assert all(getattr(m, "type", "") != "remove" for m in messages_after)
    # every message in the thread must have carried a real id through the
    # dict -> message round-trip, not None (which RemoveMessage(id=m.id) can't match)
    assert all(getattr(m, "id", None) for m in messages_after)

    # the thread must still be usable for a real turn
    out = agent.invoke({"messages": [{"role": "user", "content": "one more"}]}, config=cfg)
    assert "worked" in (out["messages"][-1].content or "")


def test_undo_does_not_delete_its_own_journal(tmp_path, fake_model):
    """The FIRST /undo of a session, in a repo with no .gitignore entry for .luna/,
    must not delete turns.json/redo.json (they're created by begin_turn AFTER pre_sha
    is captured, so without a .luna exclusion they look like turn-created files)."""
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    # deliberately NO .gitignore for .luna/, unlike this repo's own

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
    begin_turn(str(tmp_path), "sess1", len(pre))  # first turn of the session
    agent.invoke({"messages": [{"role": "user", "content": "change it"}]}, config=cfg)

    ledger = tmp_path / ".luna" / "undo" / "sess1" / "turns.json"
    assert ledger.is_file()  # sanity: it exists before undo

    undo(str(tmp_path), "sess1", agent, thread_id)

    assert ledger.is_file()  # must survive the undo that just used it


def test_begin_turn_keeps_recording_every_turn_when_luna_is_gitignored(tmp_path, fake_model):
    """CRITICAL regression: in the documented default setup (``/init`` adds
    ``.luna/`` to the project's ``.gitignore``), ``git add -A`` exits nonzero once
    ``.luna/`` exists on disk ("The following paths are ignored"), even though the
    ``:(exclude).luna/undo`` pathspec still builds a correct index. Gating
    ``_snapshot_tree`` on that exit code made ``begin_turn`` silently stop
    recording from the SECOND turn onward — a single ``/undo`` would then revert
    the whole session back to turn one instead of just the last turn. This test
    fails against the pre-fix code (only 1 ledger entry survives 3 turns; undoing
    once jumps straight from v3 to v0 instead of v3 to v2)."""
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".luna/\n")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    def _write(version):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": str(version),
                    "args": {"file_path": "/a.txt", "content": f"v{version}\n"},
                }
            ],
        )

    calls = [
        _write(1),
        AIMessage(content="1"),
        _write(2),
        AIMessage(content="2"),
        _write(3),
        AIMessage(content="3"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}

    for prompt in ("turn 1", "turn 2", "turn 3"):
        pre = agent.get_state(cfg).values.get("messages", [])
        begin_turn(str(tmp_path), "sess1", len(pre))
        agent.invoke({"messages": [{"role": "user", "content": prompt}]}, config=cfg)

    assert (tmp_path / "a.txt").read_text() == "v3\n"
    ledger = json.loads((tmp_path / ".luna" / "undo" / "sess1" / "turns.json").read_text())
    assert len(ledger) == 3  # every turn must have been recorded, not just the first

    undo(str(tmp_path), "sess1", agent, thread_id)
    assert (tmp_path / "a.txt").read_text() == "v2\n"  # one turn back, not all the way to v0


def test_undo_preserves_the_ledger_when_update_state_raises(tmp_path, fake_model):
    """M4: turns.json must not be lost if agent.update_state blows up mid-undo —
    the write is deferred until after the risky work succeeds, so a failed undo
    still leaves the turn record in place (files are still reverted; only the
    conversation truncation step, which raised, is what's rolled back to retry)."""
    import pytest
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

    real_update_state = agent.update_state

    def _boom(*a, **k):
        raise RuntimeError("boom")

    agent.update_state = _boom
    try:
        with pytest.raises(RuntimeError):
            undo(str(tmp_path), "sess1", agent, thread_id)
    finally:
        agent.update_state = real_update_state

    ledger_path = tmp_path / ".luna" / "undo" / "sess1" / "turns.json"
    turns = json.loads(ledger_path.read_text())
    assert len(turns) == 1  # the turn record must survive the failed undo
    assert (tmp_path / "a.txt").read_text() == "v0\n"  # files were still reverted


def test_undo_does_not_touch_the_users_real_index(tmp_path, fake_model):
    """Regression for M1: undo() must write to the worktree only (git restore
    --worktree), not the real git index (which `git checkout` also updates) —
    matching begin_turn's own isolated-snapshot design intent, proven for
    begin_turn by test_begin_turn_does_not_touch_the_users_index_or_worktree.

    undo() intentionally rewrites the whole *worktree* back to the turn's
    pre_sha snapshot (that's its job), so an unrelated file the user later
    staged also gets its worktree content reverted. What must NOT happen is
    the real git INDEX being rewritten too — the user's staged content for
    that file must survive in the index, exactly as `git status --porcelain`
    reports it ("MM": staged-vs-HEAD differs, worktree-vs-index also differs)
    rather than "M " (worktree matches the reverted index, the old `checkout`
    behavior)."""
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.undo import begin_turn, undo

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    (tmp_path / "b.txt").write_text("v0\n")
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

    # user has an unrelated staged change of their own, made after the turn
    (tmp_path / "b.txt").write_text("staged-change\n")
    _git(tmp_path, "add", "b.txt")

    undo(str(tmp_path), "sess1", agent, thread_id)

    # the real INDEX must still hold the user's staged content for b.txt —
    # `git show :b.txt` reads the index blob directly, bypassing the worktree
    indexed = subprocess.run(
        ["git", "show", ":b.txt"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert indexed == "staged-change\n"  # index untouched by undo
    assert (tmp_path / "a.txt").read_text() == "v0\n"  # the actual undo still happened


def test_compact_then_undo_does_not_try_to_remove_the_summary(tmp_path, fake_model):
    """After /compact truncates the conversation, undo()'s stale message_count must
    not make it try to remove messages that no longer exist (the M3 fix)."""
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.session import compact_thread
    from luna.undo import begin_turn, forget_messages, undo

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
        AIMessage(content="SUMMARY: did stuff"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}
    pre = agent.get_state(cfg).values.get("messages", [])

    begin_turn(str(tmp_path), "sess1", len(pre))
    agent.invoke({"messages": [{"role": "user", "content": "change it"}]}, config=cfg)
    assert (tmp_path / "a.txt").read_text() == "v1\n"

    import io

    from rich.console import Console

    compact_thread(agent, thread_id, Console(file=io.StringIO()))
    post_compact_count = len(agent.get_state(cfg).values["messages"])
    forget_messages(str(tmp_path), "sess1", post_compact_count)

    note = undo(str(tmp_path), "sess1", agent, thread_id)
    assert note is not None
    assert (tmp_path / "a.txt").read_text() == "v0\n"  # the file revert still worked
    messages_after = agent.get_state(cfg).values["messages"]
    assert all(getattr(m, "type", "") != "remove" for m in messages_after)
    # forget_messages reset the pending removal to 0 added messages, so the
    # compact summary itself must have survived, untouched
    assert any("SUMMARY" in (getattr(m, "content", "") or "") for m in messages_after)


def test_compact_then_double_undo_preserves_the_summary_for_every_prior_turn(tmp_path, fake_model):
    """M3 residual: forget_messages must resync EVERY existing turn's message_count,
    not just the most recent one.

    With two turns recorded before /compact, a naive `min(original, new_count)`
    clamp would leave the FIRST turn's message_count at its original, tiny,
    pre-compact value (e.g. 0) — smaller than the post-compact count — so an
    `undo()` of that earlier turn would still slice `state_messages[0:]` and
    wrongly claim the compact summary as "added by that turn," deleting it. The
    fix resets every recorded turn's message_count unconditionally, so removal is
    a genuine no-op for ANY turn that predates the compact. Traced by hand: after
    forget_messages, both turns have message_count == post_compact_count == 1, so
    both undos compute added = state_messages[1:] == [] against the 1-message
    post-compact state — the summary is never a removal candidate either time.
    """
    from langchain_core.messages import AIMessage

    from luna.agent import build_agent
    from luna.config import LunaConfig
    from luna.session import compact_thread
    from luna.undo import begin_turn, forget_messages, undo

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
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": "2",
                    "args": {"file_path": "/b.txt", "content": "hello\n"},
                }
            ],
        ),
        AIMessage(content="created b.txt"),
        AIMessage(content="SUMMARY: did stuff"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))
    thread_id = "t"
    cfg = {"configurable": {"thread_id": thread_id}}

    # turn 1: a.txt v0 -> v1
    pre1 = agent.get_state(cfg).values.get("messages", [])
    begin_turn(str(tmp_path), "sess1", len(pre1))
    agent.invoke({"messages": [{"role": "user", "content": "change a"}]}, config=cfg)
    assert (tmp_path / "a.txt").read_text() == "v1\n"

    # turn 2: create b.txt
    pre2 = agent.get_state(cfg).values["messages"]
    begin_turn(str(tmp_path), "sess1", len(pre2))
    agent.invoke({"messages": [{"role": "user", "content": "create b"}]}, config=cfg)
    assert (tmp_path / "b.txt").read_text() == "hello\n"

    import io

    from rich.console import Console

    compact_thread(agent, thread_id, Console(file=io.StringIO()))
    post_compact_count = len(agent.get_state(cfg).values["messages"])
    forget_messages(str(tmp_path), "sess1", post_compact_count)

    def _has_summary():
        return any(
            "SUMMARY" in (getattr(m, "content", "") or "")
            for m in agent.get_state(cfg).values["messages"]
        )

    # first undo: reverts turn 2 (b.txt creation) — summary must survive
    note1 = undo(str(tmp_path), "sess1", agent, thread_id)
    assert note1 is not None
    assert not (tmp_path / "b.txt").exists()
    assert (tmp_path / "a.txt").read_text() == "v1\n"  # turn 1's change untouched
    assert _has_summary()

    # second undo: reverts turn 1 (a.txt back to v0) — summary must STILL survive
    note2 = undo(str(tmp_path), "sess1", agent, thread_id)
    assert note2 is not None
    assert (tmp_path / "a.txt").read_text() == "v0\n"
    assert _has_summary()
    messages_after = agent.get_state(cfg).values["messages"]
    assert all(getattr(m, "type", "") != "remove" for m in messages_after)


def test_gc_removes_the_shadow_ref_too(tmp_path):
    import os
    import time

    from luna.undo import begin_turn, gc

    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    begin_turn(str(tmp_path), "old", message_count=0)

    old_dir = tmp_path / ".luna" / "undo" / "old"
    past = time.time() - 40 * 86400
    for f in old_dir.glob("*.json"):
        os.utime(f, (past, past))
    os.utime(old_dir, (past, past))

    gc(str(tmp_path), keep_days=7)

    ref = subprocess.run(
        ["git", "rev-parse", "refs/luna/undo/old"], cwd=tmp_path, capture_output=True, text=True
    )
    assert ref.returncode != 0  # the ref is gone
