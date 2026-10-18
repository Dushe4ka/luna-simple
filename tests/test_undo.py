import json
import os
import subprocess
import time

from luna.turn.undo import journal_dir, peek_last, session_diff, snapshot, undo_last


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


def test_undo_refuses_to_delete_a_changed_binary(tmp_path):
    from luna.turn.undo import peek_last, snapshot, undo_last

    f = tmp_path / "logo.bin"
    f.write_bytes(b"\x89PNG\x00original")
    snapshot(str(tmp_path), "s", "write_file", "logo.bin")  # before=None, existed=True
    f.write_bytes(b"\x89PNG\x00changed")
    assert "binary" in (peek_last(str(tmp_path), "s") or "").lower()
    note = undo_last(str(tmp_path), "s")
    assert f.exists() and f.read_bytes() == b"\x89PNG\x00changed"
    assert "cannot revert" in note


def test_undo_still_deletes_a_created_file(tmp_path):
    from luna.turn.undo import snapshot, undo_last

    snapshot(str(tmp_path), "s", "write_file", "new.py")  # existed=False
    (tmp_path / "new.py").write_text("x\n")
    undo_last(str(tmp_path), "s")
    assert not (tmp_path / "new.py").exists()


def test_read_paths_do_not_create_the_journal_dir(tmp_path):
    from luna.turn.undo import session_diff, undo_last

    assert session_diff(str(tmp_path), "none") == ""
    assert undo_last(str(tmp_path), "none") is None
    assert not (tmp_path / ".luna" / "undo" / "none").exists()


def test_session_diff_marks_binary_entries(tmp_path):
    from luna.turn.undo import session_diff, snapshot

    (tmp_path / "b.bin").write_bytes(b"\xff\x00\xfe")
    snapshot(str(tmp_path), "s", "edit_file", "b.bin")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    assert "binary or unreadable" in session_diff(str(tmp_path), "s")


def test_gc_removes_old_journals(tmp_path):
    from luna.turn.undo import gc, journal_dir

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
    from luna.turn.undo import gc, journal_dir

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
    from luna.turn.undo import begin_turn

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
    from luna.turn.undo import begin_turn

    begin_turn(str(tmp_path), "sess1", message_count=1)  # must not raise
    assert not (tmp_path / ".luna" / "undo").exists()


def test_begin_turn_handles_an_unborn_head(tmp_path):
    from luna.turn.undo import begin_turn

    _git(tmp_path, "init", "-q")  # no commits yet
    begin_turn(str(tmp_path), "sess1", message_count=0)  # must not raise
    ledger = tmp_path / ".luna" / "undo" / "sess1" / "turns.json"
    assert ledger.is_file()


def test_snapshot_tree_returns_none_on_a_genuine_add_failure_not_an_empty_tree(tmp_path):
    """A GENUINE `git add` failure (e.g. an unreadable file) must not produce an
    EMPTY tree that a later undo() would read as "every file was added by this
    turn" and delete. `_snapshot_tree` tells this apart from the benign
    ignored-paths warning by exit code (0/1 is fine, anything else — e.g. 128,
    "fatal: adding files failed" — is a real failure), not by whether the temp
    index file exists: a brand-new repo with nothing to stage also never writes
    one, on a perfectly successful exit 0."""
    import stat

    from luna.turn.undo import _snapshot_tree

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


def test_snapshot_tree_still_detects_failure_under_add_ignore_errors(tmp_path):
    """With the repo's ``add.ignoreErrors`` set to true (a real, if unusual, git
    config a user or CI environment might have), a genuine indexing failure exits
    1 instead of 128 while still leaving the failed path out of the index —
    exactly the exit code `_snapshot_tree` otherwise treats as the benign
    ignored-paths warning. `_snapshot_tree` pins `add.ignoreErrors=false` on its
    own invocation so this doesn't slip through as a falsely "successful" (but
    silently incomplete) snapshot."""
    import stat

    from luna.turn.undo import _snapshot_tree

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "add.ignoreErrors", "true")
    (tmp_path / "keep.txt").write_text("keep\n")
    (tmp_path / "locked.txt").write_text("locked\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    locked = tmp_path / "locked.txt"
    locked.chmod(0o000)
    try:
        tree = _snapshot_tree(str(tmp_path))
    finally:
        locked.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert tree is None


def test_begin_turn_does_not_touch_the_users_index_or_worktree(tmp_path):
    from luna.turn.undo import begin_turn

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


def test_undo_with_no_turns_returns_none(tmp_path):
    from luna.turn.undo import undo

    _git(tmp_path, "init", "-q")
    assert undo(str(tmp_path), "sess-empty", agent=None, thread_id="t") is None


def test_redo_with_nothing_to_redo_returns_none(tmp_path):
    from luna.turn.undo import redo

    _git(tmp_path, "init", "-q")
    assert redo(str(tmp_path), "sess-empty", agent=None, thread_id="t") is None


def test_gc_removes_the_shadow_ref_too(tmp_path):
    import os
    import time

    from luna.turn.undo import begin_turn, gc

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
