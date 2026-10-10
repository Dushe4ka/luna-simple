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
