import subprocess

from luna.gitinfo import dirty_paths, is_git_repo


def test_not_a_repo(tmp_path):
    assert is_git_repo(str(tmp_path)) is False
    assert dirty_paths(str(tmp_path)) == []


def test_dirty_detection(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x")
    assert is_git_repo(str(tmp_path)) is True
    assert "a.txt" in " ".join(dirty_paths(str(tmp_path)))


def _git(tmp_path, *args):
    subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)


def test_dirty_paths_handles_a_rename_without_a_stray_arrow(tmp_path):
    """Regression for the C1 fix: a `git status --porcelain` rename entry reads
    as "R  old.txt -> new.txt" under the old plain-text parsing, which naive
    line[3:] slicing turned into a single 3-word "path" containing " -> ". The
    ``-z``-based parser must instead yield a clean single new-path entry."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "old.txt").write_text("content\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    (tmp_path / "old.txt").rename(tmp_path / "new.txt")
    _git(tmp_path, "add", "-A")

    paths = dirty_paths(str(tmp_path))
    assert "new.txt" in paths
    assert not any(" -> " in p for p in paths)
    assert not any(p.count(" ") > 0 and "old.txt" in p and "new.txt" in p for p in paths)


def test_dirty_paths_handles_a_modified_tracked_file_as_the_first_entry(tmp_path):
    """Regression found during self-review of the C1 fix: `git status --porcelain
    -z`'s first two columns are a fixed-width status code that can legitimately
    start with a space (" M" = modified in the worktree, not staged). ``_run``'s
    ``str.strip()`` would eat that leading space when it lands at the very start
    of stdout, shifting the entry's status/path split by one character and
    corrupting the path (e.g. "a.txt" -> ".txt"). dirty_paths must not go through
    a stripping code path for this output."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    (tmp_path / "a.txt").write_text("v1\n")  # modified, unstaged: porcelain status " M"

    paths = dirty_paths(str(tmp_path))
    assert paths == ["a.txt"]


def test_dirty_paths_never_executes_shell_metacharacters_in_a_filename(tmp_path):
    """Regression for the C1 fix: dirty_paths itself must not choke on or split
    a path containing shell metacharacters — it's a pure parser, the injection
    risk lives downstream in fmt.run/diagnose.run which now shlex.quote every
    path this function returns."""
    _git(tmp_path, "init", "-q")
    evil_name = "x$(touch PWNED).py"
    (tmp_path / evil_name).write_text("x\n")

    paths = dirty_paths(str(tmp_path))
    assert evil_name in paths
    assert not (tmp_path / "PWNED").exists()
