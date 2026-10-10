import subprocess
from pathlib import Path

import pytest
from luna_core import skills
from luna_core.memory import append_note, memory_files, remember
from luna_core.skills import SkillError
from luna_core.tools.base import ToolContext
from luna_core.workspace import Workspace


def write_skill(root: Path, name: str, desc: str, body: str = "do it") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n")
    return d


def test_frontmatter_is_real_yaml():
    meta, body = skills.parse_frontmatter(
        '---\nname: x\ndescription: "a: b # not a comment"\n---\nBody\n'
    )
    assert meta == {"name": "x", "description": "a: b # not a comment"} and body.strip() == "Body"
    assert skills.parse_frontmatter("no frontmatter") == ({}, "no frontmatter")


def test_discover_project_overrides_user(tmp_path):
    user, proj = tmp_path / "user", tmp_path / "proj"
    write_skill(user, "deploy", "user deploy")
    write_skill(user, "lint", "lint things")
    write_skill(proj, "deploy", "project deploy")
    found = {
        s.name: s
        for s in skills.discover(
            [("user", user), ("project", proj), ("project", tmp_path / "missing")]
        )
    }
    assert found["deploy"].description == "project deploy" and found["deploy"].scope == "project"
    assert found["lint"].scope == "user"
    assert skills.load_body("deploy", [("user", user), ("project", proj)]).strip() == "do it"


def test_save_round_trips_tricky_descriptions(tmp_path):
    path = skills.save("fix-ci", 'Fix CI: "ruff" + tests # always', "1. run ruff", tmp_path)
    meta, body = skills.parse_frontmatter(path.read_text())
    assert (
        meta["description"] == 'Fix CI: "ruff" + tests # always' and body.strip() == "1. run ruff"
    )
    with pytest.raises(SkillError):
        skills.save("bad name!", "x", "y", tmp_path)
    with pytest.raises(SkillError):
        skills.save("ok", "two\nlines", "y", tmp_path)


def test_install_from_a_local_git_repo(tmp_path):
    repo = tmp_path / "repo"
    write_skill(repo / "skills", "pdf", "read pdfs")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=repo,
        check=True,
    )
    dest = tmp_path / "installed"
    msg = skills.install(str(repo / "skills" / "pdf"), dest)
    assert "installed skill 'pdf'" in msg and (dest / "pdf" / "SKILL.md").is_file()
    assert skills.remove("pdf", [("user", dest)]) and not (dest / "pdf").exists()


async def test_skill_and_save_skill_tools(tmp_path):
    write_skill(tmp_path / "s", "deploy", "ship it", "step one")
    tool = skills.skill_tool(lambda: [("project", tmp_path / "s")])
    out = await tool.invoke(ToolContext(), {"name": "deploy"})
    assert "step one" in out.content[0].text
    missing = await tool.invoke(ToolContext(), {"name": "nope"})
    assert missing.is_error and "deploy" in missing.content[0].text
    saver = skills.save_skill_tool(lambda project: tmp_path / ("p" if project else "u"))
    assert saver.mutating
    res = await saver.invoke(ToolContext(), {"name": "x", "description": "d", "body": "b"})
    assert not res.is_error and (tmp_path / "p" / "x" / "SKILL.md").is_file()


async def test_memory_notes_and_remember_tool(tmp_path):
    path = append_note(tmp_path, "failures", "pip", "pip install breaks the venv")
    assert "## " in path.read_text() and "pip install breaks" in path.read_text()
    assert memory_files(tmp_path) == [".luna/memory/failures.md"]
    with pytest.raises(ValueError):
        append_note(tmp_path, "random", "x", "y")
    out = await remember.invoke(
        ToolContext(workspace=Workspace(tmp_path)),
        {"kind": "decisions", "topic": "db", "note": "sqlite"},
    )
    assert (
        not out.is_error
        and (tmp_path / ".luna" / "memory" / "decisions.md").is_file()
        and remember.mutating
    )
