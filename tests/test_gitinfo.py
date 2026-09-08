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
