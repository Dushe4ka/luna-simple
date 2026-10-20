import shutil
import subprocess

import pytest
from luna_core.undo import UndoJournal, gc


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_git_undo_redo_restores_and_removes_created_files(repo):
    j = UndoJournal(repo, "s1")
    assert j.is_git and j.depth == 0
    j.begin_turn("t1")
    for rel in ("a.txt", "new.txt"):
        j.before_write("t1", rel)
    (repo / "a.txt").write_text("two\n")
    (repo / "new.txt").write_text("created\n")
    assert j.depth == 1
    res = j.undo()
    assert (
        res.turn_id == "t1"
        and (repo / "a.txt").read_text() == "one\n"
        and not (repo / "new.txt").exists()
    )
    assert j.depth == 0 and j.redo_depth == 1
    assert j.redo().turn_id == "t1"
    assert (repo / "a.txt").read_text() == "two\n" and (repo / "new.txt").read_text() == "created\n"
    assert j.undo() is not None and j.undo() is None


def test_git_redo_is_cleared_by_a_new_turn_and_index_is_untouched(repo):
    j = UndoJournal(repo, "s1")
    j.begin_turn("t1")
    (repo / "a.txt").write_text("two\n")
    j.undo()
    j.begin_turn("t2")
    assert j.redo_depth == 0
    status = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert status == ""


def test_journal_without_git(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    j = UndoJournal(tmp_path, "s1")
    assert not j.is_git
    j.begin_turn("t1")
    j.before_write("t1", "a.txt")
    (tmp_path / "a.txt").write_text("two\n")
    j.before_write("t1", "a.txt")  # second write in the same turn keeps the first pre-image
    (tmp_path / "a.txt").write_text("three\n")
    j.before_write("t1", "b.txt")
    (tmp_path / "b.txt").write_text("new\n")
    assert "-one" in j.diff() and "+three" in j.diff()
    res = j.undo()
    assert (
        res.turn_id == "t1"
        and (tmp_path / "a.txt").read_text() == "one\n"
        and not (tmp_path / "b.txt").exists()
    )
    j.redo()
    assert (tmp_path / "a.txt").read_text() == "three\n" and (
        tmp_path / "b.txt"
    ).read_text() == "new\n"


def test_turn_without_changes_still_counts(tmp_path):
    j = UndoJournal(tmp_path, "s1")
    j.begin_turn("t1")
    assert j.depth == 1 and j.undo().note == "no file changes in this turn"


def test_gc_keeps_the_named_session(tmp_path):
    for sid in ("a", "b", "c"):
        UndoJournal(tmp_path, sid).begin_turn("t")
    gc(tmp_path, keep_max=1, keep="a")
    left = sorted(p.name for p in (tmp_path / ".luna" / "undo").iterdir())
    assert "a" in left and len(left) == 2


# -- review finding 3: undo must not claim success when it could not restore --


def test_undo_reports_failure_when_snapshot_is_missing(repo, monkeypatch):
    import luna_core.undo as undo_mod

    j = UndoJournal(repo, "s1")
    j.begin_turn("t1")
    (repo / "a.txt").write_text("two\n")
    monkeypatch.setattr(undo_mod, "_snapshot_tree", lambda root: None)
    res = j.undo()
    assert res.note == "files could not be restored (no snapshot of the turn)"
    # the turn record is still rewound even though nothing could be restored
    assert j.depth == 0 and j.redo_depth == 1


def test_undo_notes_files_that_could_not_be_restored(tmp_path):
    (tmp_path / "d").mkdir()
    j = UndoJournal(tmp_path, "s1")
    j.begin_turn("t1")
    j.before_write("t1", "d")
    shutil.rmtree(tmp_path / "d")
    (tmp_path / "d").write_text("now a file\n")
    res = j.undo()
    assert "could not restore" in res.note and "d" in res.note


# -- review finding 4: non-git pre-images must round-trip bytes exactly --


def test_journal_restores_crlf_and_binary_bytes_exact(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\xff\xfe")
    j = UndoJournal(tmp_path, "s1")
    j.begin_turn("t1")
    j.before_write("t1", "a.txt")
    j.before_write("t1", "b.bin")
    (tmp_path / "a.txt").write_bytes(b"changed\n")
    (tmp_path / "b.bin").write_bytes(b"also changed")
    j.undo()
    assert (tmp_path / "a.txt").read_bytes() == b"one\r\ntwo\r\n"
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\x01\xff\xfe"


# -- review finding (task 17): top_turn/discard_top/top_redo let a caller
# -- skip a journal record that has no matching visible conversation turn --


def test_top_turn_and_discard_top(repo):
    j = UndoJournal(repo, "s1")
    assert j.top_turn() is None
    j.begin_turn("t1")
    assert j.top_turn() == "t1"
    j.begin_turn("t2")
    assert j.top_turn() == "t2" and j.depth == 2
    j.discard_top()
    assert j.top_turn() == "t1" and j.depth == 1
    (repo / "a.txt").write_text("still there\n")
    assert (repo / "a.txt").read_text() == "still there\n"  # discard_top never touches files
    j.discard_top()
    assert j.top_turn() is None and j.depth == 0
    j.discard_top()  # discarding an empty journal is a no-op
    assert j.depth == 0


def test_top_redo(repo):
    j = UndoJournal(repo, "s1")
    assert j.top_redo() is None
    j.begin_turn("t1")
    j.undo()
    assert j.top_redo() == "t1"
    j.redo()
    assert j.top_redo() is None


def test_parallel_before_write_keeps_every_pre_image(tmp_path):
    """Parallel tool calls journal concurrently: no lost update, no tmp-file race."""
    from concurrent.futures import ThreadPoolExecutor

    names = [f"f{i}.txt" for i in range(16)]
    for name in names:
        (tmp_path / name).write_text(f"old {name}\n")
    journal = UndoJournal(tmp_path, "s")
    journal.begin_turn("t1")
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda n: UndoJournal(tmp_path, "s").before_write("t1", n), names))
    for name in names:
        (tmp_path / name).write_text("new\n")
    journal.undo()
    assert all((tmp_path / n).read_text() == f"old {n}\n" for n in names)


# -- final review C1: undo/redo touch only what the turn changed and never
# -- overwrite a path the user edited since --


@pytest.fixture
def repo2(repo):
    (repo / "b.txt").write_text("bee\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "b")
    return repo


def test_git_undo_keeps_user_changes_made_after_the_turn(repo2):
    j = UndoJournal(repo2, "s1")
    j.begin_turn("t1")
    j.before_write("t1", "a.txt")
    (repo2 / "a.txt").write_text("two\n")
    j.end_turn("t1")
    (repo2 / "b.txt").write_text("user edit\n")
    (repo2 / "notes.md").write_text("my notes\n")
    res = j.undo()
    assert (repo2 / "a.txt").read_text() == "one\n"
    assert (repo2 / "b.txt").read_text() == "user edit\n"
    assert (repo2 / "notes.md").read_text() == "my notes\n"
    assert "skipped" not in res.note
    j.redo()
    assert (repo2 / "a.txt").read_text() == "two\n"
    assert (repo2 / "b.txt").read_text() == "user edit\n"
    assert (repo2 / "notes.md").read_text() == "my notes\n"


def test_git_undo_skips_a_turn_path_the_user_changed_since(repo2):
    j = UndoJournal(repo2, "s1")
    j.begin_turn("t1")
    for rel in ("a.txt", "new.txt"):
        j.before_write("t1", rel)
    (repo2 / "a.txt").write_text("two\n")
    (repo2 / "new.txt").write_text("created\n")
    j.end_turn("t1")
    (repo2 / "a.txt").write_text("user edit\n")
    res = j.undo()
    assert (repo2 / "a.txt").read_text() == "user edit\n"
    assert not (repo2 / "new.txt").exists()
    assert "skipped a.txt: changed since the turn" in res.note
    # redo skips it too: its content is no longer the turn's pre-image
    res = j.redo()
    assert (repo2 / "a.txt").read_text() == "user edit\n"
    assert (repo2 / "new.txt").read_text() == "created\n"
    assert "skipped a.txt: changed since" in res.note


def test_git_redo_skips_a_path_the_user_changed_after_the_undo(repo2):
    j = UndoJournal(repo2, "s1")
    j.begin_turn("t1")
    for rel in ("a.txt", "b.txt"):
        j.before_write("t1", rel)
    (repo2 / "a.txt").write_text("two\n")
    (repo2 / "b.txt").write_text("turn b\n")
    j.end_turn("t1")
    j.undo()
    (repo2 / "a.txt").write_text("user edit\n")
    res = j.redo()
    assert (repo2 / "a.txt").read_text() == "user edit\n"
    assert (repo2 / "b.txt").read_text() == "turn b\n"
    assert "skipped a.txt" in res.note


def test_journal_undo_and_redo_skip_files_the_user_edited(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    (tmp_path / "b.txt").write_text("bee\n")
    j = UndoJournal(tmp_path, "s1")
    j.begin_turn("t1")
    for rel in ("a.txt", "b.txt"):
        j.before_write("t1", rel)
        (tmp_path / rel).write_text(f"turn {rel}\n")
    j.end_turn("t1")
    (tmp_path / "b.txt").write_text("user edit\n")
    res = j.undo()
    assert (tmp_path / "a.txt").read_text() == "one\n"
    assert (tmp_path / "b.txt").read_text() == "user edit\n"
    assert "skipped b.txt: changed since the turn" in res.note
    (tmp_path / "a.txt").write_text("user again\n")
    res = j.redo()
    assert (tmp_path / "a.txt").read_text() == "user again\n"
    assert (tmp_path / "b.txt").read_text() == "user edit\n"
    assert "skipped" in res.note and "a.txt" in res.note


def test_journal_undo_survives_an_oserror_mid_restore(tmp_path, monkeypatch):
    for rel in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / rel).write_text(f"old {rel}\n")
    j = UndoJournal(tmp_path, "s1")
    j.begin_turn("t1")
    for rel in ("a.txt", "b.txt", "c.txt"):
        j.before_write("t1", rel)
        (tmp_path / rel).write_text("new\n")
    j.end_turn("t1")
    real = UndoJournal._write

    def flaky(self, rel, image):
        if rel == "b.txt":
            raise PermissionError("read-only")
        return real(self, rel, image)

    monkeypatch.setattr(UndoJournal, "_write", flaky)
    res = j.undo()
    assert (tmp_path / "a.txt").read_text() == "old a.txt\n"
    assert (tmp_path / "c.txt").read_text() == "old c.txt\n"
    assert (tmp_path / "b.txt").read_text() == "new\n"
    assert "could not restore" in res.note and "b.txt" in res.note
    # the journal was saved: the turn moved to the redo stack
    fresh = UndoJournal(tmp_path, "s1")
    assert fresh.depth == 0 and fresh.redo_depth == 1
    monkeypatch.setattr(UndoJournal, "_write", real)
    fresh.redo()
    assert (tmp_path / "a.txt").read_text() == "new\n"
    assert (tmp_path / "b.txt").read_text() == "new\n"


# -- re-review R1: in git mode only paths touched by file tools are restored;
# -- a turn that ran commands restores the whole diff but names the extra paths --


def test_git_undo_leaves_files_edited_outside_luna_during_the_turn(repo2):
    j = UndoJournal(repo2, "s1")
    j.begin_turn("t1")
    j.before_write("t1", "a.txt")
    (repo2 / "a.txt").write_text("two\n")
    (repo2 / "b.txt").write_text("user edit during the turn\n")  # the user's editor
    j.end_turn("t1")
    res = j.undo()
    assert (repo2 / "a.txt").read_text() == "one\n"
    assert (repo2 / "b.txt").read_text() == "user edit during the turn\n"
    assert res.note == "files restored"
    j.redo()
    assert (repo2 / "a.txt").read_text() == "two\n"
    assert (repo2 / "b.txt").read_text() == "user edit during the turn\n"


def test_git_undo_of_a_turn_with_commands_restores_and_names_the_extra_paths(repo2):
    j = UndoJournal(repo2, "s1")
    j.begin_turn("t1")
    j.before_write("t1", "a.txt")
    (repo2 / "a.txt").write_text("two\n")
    j.note_command("t1")
    (repo2 / "b.txt").write_text("changed by a command\n")
    j.end_turn("t1")
    res = j.undo()
    assert (repo2 / "a.txt").read_text() == "one\n"
    assert (repo2 / "b.txt").read_text() == "bee\n"
    assert "also restored (changed by commands or outside Luna): b.txt" in res.note
    assert "a.txt" not in res.note


def test_git_undo_without_a_recorded_end_restores_only_tool_paths(repo2):
    j = UndoJournal(repo2, "s1")
    j.begin_turn("t1")
    j.before_write("t1", "a.txt")
    (repo2 / "a.txt").write_text("two\n")
    (repo2 / "b.txt").write_text("user edit\n")
    j.note_command("t1")  # even with commands: without an end, "now" is not the turn's end
    res = j.undo()  # no end_turn: the process died mid-turn
    assert (repo2 / "a.txt").read_text() == "one\n"
    assert (repo2 / "b.txt").read_text() == "user edit\n"
    assert "also restored" not in res.note
