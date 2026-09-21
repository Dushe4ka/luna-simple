import subprocess

import pytest

from luna.config.providers import LunaConfigError
from luna.extensions.skills import install, list_skills, remove, save


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _fake_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "greet").mkdir(parents=True)
    (repo / "greet" / "SKILL.md").write_text(
        "---\nname: greet\ndescription: say hi nicely\n---\n\nSay hi.\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x")
    return repo


def test_install_from_local_repo_subdir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    repo = _fake_repo(tmp_path)
    msg = install(f"{repo}/greet", workdir=str(tmp_path))
    dest = tmp_path / ".config" / "luna" / "skills" / "greet" / "SKILL.md"
    assert dest.is_file()
    assert "/reload" in msg
    assert ("user", "greet", "say hi nicely") in list_skills(str(tmp_path))


def test_install_rejects_skill_without_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    repo = tmp_path / "bad"
    (repo / "x").mkdir(parents=True)
    (repo / "x" / "SKILL.md").write_text("no frontmatter here")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x")
    with pytest.raises(LunaConfigError):
        install(f"{repo}/x", workdir=str(tmp_path))
    assert not (tmp_path / ".config" / "luna" / "skills" / "x").exists()


def test_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    repo = _fake_repo(tmp_path)
    install(f"{repo}/greet", workdir=str(tmp_path))
    assert remove("greet", workdir=str(tmp_path)) is True
    assert remove("greet", workdir=str(tmp_path)) is False


def test_save_writes_a_skill_that_list_skills_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    path = save(
        "fix-flaky-retry",
        "retries a flaky step with backoff",
        "1. Detect the flaky step.\n2. Wrap it with a 3-attempt retry.\n",
        workdir=str(tmp_path),
    )
    assert path.is_file()
    assert path == tmp_path / ".luna" / "skills" / "fix-flaky-retry" / "SKILL.md"
    assert ("project", "fix-flaky-retry", "retries a flaky step with backoff") in list_skills(
        str(tmp_path)
    )


def test_save_user_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    path = save("greet", "say hi", "Say hi.\n", project=False, workdir=str(tmp_path))
    assert path == tmp_path / ".config" / "luna" / "skills" / "greet" / "SKILL.md"


def test_save_rejects_unsafe_names(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    for bad in ("../escape", "a/b", "", "  ", "a b", "ok\n"):
        with pytest.raises(LunaConfigError):
            save(bad, "desc", "body", workdir=str(tmp_path))
    assert not (tmp_path / ".luna" / "skills").exists()


def test_save_rejects_empty_or_multiline_description(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    with pytest.raises(LunaConfigError):
        save("x", "", "body", workdir=str(tmp_path))
    with pytest.raises(LunaConfigError):
        save("x", "line one\nline two", "body", workdir=str(tmp_path))


def test_save_overwrites_an_existing_same_named_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    save("x", "first version", "first body", workdir=str(tmp_path))
    save("x", "second version", "second body", workdir=str(tmp_path))
    entries = list_skills(str(tmp_path))
    assert [e for e in entries if e[1] == "x"] == [("project", "x", "second version")]
