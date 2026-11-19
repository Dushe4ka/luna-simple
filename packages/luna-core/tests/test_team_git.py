"""Git plumbing of a team run: team branch, member worktrees, serial merges."""

import subprocess

import pytest
from luna_core.team.git import TeamGit, TeamGitError


def git(root, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    return root


def test_start_creates_the_team_branch_and_leaves_the_user_tree_alone(repo):
    (repo / "a.txt").write_text("dirty\n")
    (repo / "mine.txt").write_text("untracked\n")
    tg = TeamGit(repo, "r1")
    base = tg.start()
    assert tg.tip() == base == git(repo, "rev-parse", "HEAD").strip()
    assert (tg.integration / "a.txt").read_text() == "one\n"
    assert not (tg.integration / "mine.txt").exists()
    assert (repo / "a.txt").read_text() == "dirty\n"
    assert git(repo, "branch", "--show-current").strip() == "main"
    with pytest.raises(TeamGitError, match="already exists"):
        TeamGit(repo, "r1").start()


def test_start_needs_a_commit(tmp_path):
    git(tmp_path, "init", "-q")
    with pytest.raises(TeamGitError, match="no commits"):
        TeamGit(tmp_path, "r1").start()


def test_member_work_merges_serially(repo):
    tg = TeamGit(repo, "r1")
    tg.start()
    t1 = tg.member_tree("T1", 1)
    assert t1.branch == "luna/teamwork/r1/T1"
    (t1.path / "b.txt").write_text("b\n")
    stat, commits = tg.finish(t1, "T1")
    assert "b.txt" in stat and len(commits) == 1 and not t1.path.exists()
    result = tg.merge(t1.branch, "merge T1")
    assert result.status == "merged" and result.commit == tg.tip()
    assert (tg.integration / "b.txt").exists()
    assert git(repo, "show", f"{tg.branch}:b.txt") == "b\n"
    assert tg.merge(t1.branch, "again").status == "empty"


def test_conflicting_work_is_reported_and_resolved_in_a_merge_tree(repo):
    tg = TeamGit(repo, "r1")
    tg.start()
    t1, t2 = tg.member_tree("T1", 1), tg.member_tree("T2", 1)
    (t1.path / "a.txt").write_text("from one\n")
    (t2.path / "a.txt").write_text("from two\n")
    tg.finish(t1, "T1")
    tg.finish(t2, "T2")
    assert tg.merge(t1.branch, "m1").status == "merged"
    conflict = tg.merge(t2.branch, "m2")
    assert conflict.status == "conflict" and "a.txt" in conflict.detail
    assert git(repo, "-C", str(tg.integration), "status", "--porcelain") == ""
    fix = tg.member_tree("T3", 1, merge=t2.branch)
    assert "<<<<<<<" in (fix.path / "a.txt").read_text()
    tg.finish(fix, "T3 unresolved")
    markers = tg.merge(fix.branch, "m3")
    assert markers.status == "markers" and "a.txt" in markers.detail
    retry = tg.member_tree("T3", 2, branch=fix.branch)
    (retry.path / "a.txt").write_text("from both\n")
    tg.finish(retry, "T3")
    assert tg.merge(retry.branch, "m3").status == "merged"
    assert git(repo, "show", f"{tg.branch}:a.txt") == "from both\n"


def test_verify_and_undo(repo):
    tg = TeamGit(repo, "r1")
    tg.start()
    t1 = tg.member_tree("T1", 1)
    (t1.path / "b.txt").write_text("b\n")
    tg.finish(t1, "T1")
    result = tg.merge(t1.branch, "m1")
    assert tg.verify("test -f b.txt", 30) == (True, "")
    ok, output = tg.verify("echo boom && exit 3", 30)
    assert not ok and "boom" in output
    tg.undo_merge(result.before)
    assert tg.tip() == result.before and not (tg.integration / "b.txt").exists()


def test_an_empty_attempt_leaves_no_branch(repo):
    tg = TeamGit(repo, "r1")
    tg.start()
    t1 = tg.member_tree("T1", 1)
    assert tg.finish(t1, "T1") == ("", [])
    assert not tg.has_branch(t1.branch)


def test_salvage_close_and_ensure(repo):
    tg = TeamGit(repo, "r1")
    base = tg.start()
    t1 = tg.member_tree("T1", 1)
    (t1.path / "c.txt").write_text("c\n")
    assert "c.txt" in tg.salvage(t1.path, t1.branch)
    assert not t1.path.exists() and tg.has_branch(t1.branch)
    tg.delete_branch(t1.branch)
    assert not tg.has_branch(t1.branch)
    tg.close()
    assert not tg.integration.exists() and tg.has_branch(tg.branch)
    tg.ensure()
    assert (tg.integration / "a.txt").exists()
    assert tg.diffstat(base) == ""
