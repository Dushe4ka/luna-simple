"""The team runner: members, merges, the lead and the report."""

import asyncio
import subprocess

import pytest
from luna_core.assemblies import AssemblyLog
from luna_core.policy import Rules
from luna_core.team.settings import TeamSettings
from team_kit import (
    TeamProvider,
    call,
    done,
    git,
    make_repo,
    make_runner,
    plan_dict,
    ready_run,
    report_of,
    result_of,
    text,
)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


async def test_team_merges_code_writes_notes_and_reports(repo, tmp_path):
    (repo / "mine.txt").write_text("user work\n")
    provider = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
                call("read_file", {"file_path": "b.txt"}, "r1"),
                done("b holds one line"),
                text("done"),
            ]
        }
    )
    journal = AssemblyLog(tmp_path / "db" / "s.db")
    runner, store, log = make_runner(tmp_path, provider, journal=journal)
    rid = ready_run(
        runner,
        store,
        plan_dict(
            [
                {"id": "T1", "title": "write b", "kind": "code", "member": "dev"},
                {
                    "id": "T2",
                    "title": "check b",
                    "kind": "research",
                    "deps": ["T1"],
                    "member": "dev",
                },
            ]
        ),
    )
    head = git(repo, "rev-parse", "HEAD")
    assert await runner.run(rid) == "done"
    assert git(repo, "show", f"luna/team/{rid}:b.txt") == "b\n"
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "branch", "--show-current").strip() == "main"
    assert not (repo / "b.txt").exists() and (repo / "mine.txt").read_text() == "user work\n"
    note = repo / ".luna" / "teams" / rid / "notes" / "T2.md"
    assert "b holds one line" in note.read_text(encoding="utf-8")
    report = report_of(repo, rid)
    assert "**Итог:** ГОТОВО" in report and f"git merge --no-ff luna/team/{rid}" in report
    assert list((repo / ".luna" / "worktrees").glob("team-*")) == []
    assert git(repo, "branch", "--list", "luna/teamwork/*").strip() == ""
    assert provider.requests["lead"][0].model == "lead-model"
    assert provider.requests["dev"][0].model == "main-model"
    assert {t.name for t in provider.requests["dev"][3].tools} >= {"read_file", "board_update"}
    assert "write_file" not in {t.name for t in provider.requests["dev"][3].tools}
    assert [r.base_profile for r in journal.pending()] == ["team", "team"]
    run = store.run(rid)
    assert (run.status, run.outcome) == ("finished", "done")
    assert len(store.events(rid, kinds=["team_done"])) == 1
    journal.close()
    log.close()
    store.close()


def writer(content: str) -> list:
    return [
        call("read_file", {"file_path": "a.txt"}, "r"),
        call("write_file", {"file_path": "a.txt", "content": content}, "w"),
        done(content.strip()),
        text("done"),
    ]


async def test_a_merge_conflict_becomes_a_resolve_task(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": writer("from dev\n") + writer("both\n"),
            "ops": writer("from ops\n") + writer("both\n"),
        },
        delay=0.05,
    )
    runner, store, log = make_runner(tmp_path, provider, settings=TeamSettings(max_concurrent=2))
    rid = ready_run(
        runner,
        store,
        plan_dict(
            [
                {"id": "T1", "title": "dev edit", "member": "dev"},
                {"id": "T2", "title": "ops edit", "member": "ops"},
            ],
            members=[
                {"name": "dev", "prompt": "You edit."},
                {"name": "ops", "prompt": "You edit."},
            ],
        ),
    )
    assert await runner.run(rid) == "done"
    tasks = store.tasks(rid)
    assert [t.status for t in tasks] == ["done", "done", "done"]
    fix = tasks[2]
    assert fix.merge_of in ("T1", "T2") and fix.title.startswith("resolve merge conflict")
    assert git(repo, "show", f"luna/team/{rid}:a.txt") == "both\n"
    assert len(store.events(rid, kinds=["conflict"])) == 1
    assert "конфликт при вливании" in report_of(repo, rid)
    log.close()
    store.close()


VERIFY = "if [ -f bad.txt ]; then echo bad file present; exit 1; fi"


async def test_red_verify_sends_the_task_back_with_the_output(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "bad.txt", "content": "x\n"}, "w1"),
                done("first try"),
                text("done"),
                call("delete", {"file_path": "bad.txt"}, "d1"),
                done("removed it"),
                text("done"),
            ]
        }
    )
    settings = TeamSettings(verify_command=VERIFY, max_attempts=2)
    runner, store, log = make_runner(tmp_path, provider, settings=settings)
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "tidy", "member": "dev"}]))
    assert await runner.run(rid) == "done"
    assert len(store.events(rid, kinds=["verify_failed"])) == 1
    second = provider.requests["dev"][3].messages[0].text
    assert "verify failed after merging" in second and "bad file present" in second
    shown = subprocess.run(
        ["git", "cat-file", "-e", f"luna/team/{rid}:bad.txt"], cwd=repo, capture_output=True
    )
    assert shown.returncode != 0
    assert store.task(rid, "T1").attempts == 2
    log.close()
    store.close()


async def test_denied_commands_land_in_the_report(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("execute", {"command": "curl https://example.com"}, "e1"),
                call("execute", {"command": "git push origin main"}, "e2"),
                call("execute", {"command": "git status"}, "e3"),
                done("checked"),
                text("done"),
            ]
        }
    )
    runner, store, log = make_runner(
        tmp_path, provider, base_rules=Rules(deny=["execute:git push*"])
    )
    rid = ready_run(
        runner,
        store,
        plan_dict([{"id": "T1", "title": "look", "member": "dev"}], allow=["execute:git *"]),
    )
    assert await runner.run(rid) == "done"
    denied = [
        (e.data["preview"], e.data["reason"]) for e in store.events(rid, kinds=["team_denied"])
    ]
    assert denied == [("curl https://example.com", "profile"), ("git push origin main", "rule")]
    report = report_of(repo, rid)
    assert "⊘ dev: `curl https://example.com` — не в профиле" in report
    assert "⊘ dev: `git push origin main` — запрещено правилом permissions.toml" in report
    assert result_of(provider.requests["dev"][3], "e3").is_error is False
    log.close()
    store.close()


async def test_lead_wakes_on_messages_and_changes_and_can_add_work(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("send_message", {"to": "lead", "text": "T1 needs a README too"}, "m1"),
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("b"),
                text("done"),
                call("write_file", {"file_path": "README.md", "content": "# b\n"}, "w2"),
                done("readme"),
                text("done"),
            ],
            "lead": [
                call("board_add", {"title": "write README", "kind": "code", "member": "dev"}, "a1"),
                text("added"),
            ],
        }
    )
    runner, store, log = make_runner(tmp_path, provider)
    rid = ready_run(
        runner,
        store,
        plan_dict([{"id": "T1", "title": "write b", "kind": "code", "member": "dev"}]),
    )
    assert await runner.run(rid) == "done"
    assert [t.id for t in store.tasks(rid)] == ["T1", "T2"]
    assert git(repo, "show", f"luna/team/{rid}:README.md") == "# b\n"
    first_wake = provider.requests["lead"][0].messages[-1].text
    assert "from dev: T1 needs a README too" in first_wake and "Board:" in first_wake
    assert "Team lead" in provider.requests["lead"][0].system
    decisions = [e.data["text"] for e in store.events(rid, kinds=["decision"])]
    assert decisions == ["added T2: write README"]
    log.close()
    store.close()


async def test_a_member_that_reports_failed_leaves_a_partial_run(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("board_update", {"status": "failed", "note": "cannot reach the api"}, "u"),
                text("sorry"),
            ]
        }
    )
    runner, store, log = make_runner(tmp_path, provider)
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "call api", "member": "dev"}]))
    assert await runner.run(rid) == "partial"
    assert store.task(rid, "T1").note == "cannot reach the api"
    assert "**Итог:** ЧАСТИЧНО 0/1" in report_of(repo, rid)
    log.close()
    store.close()


async def test_no_member_tree_forks_from_a_merge_under_verification(repo, tmp_path):
    # ops finishes a research task only once T1's verify has started (the verify
    # command touches ``mark`` and then sleeps), so ops claims T2 right inside
    # the merge→verify→undo window of T1's rejected merge.
    mark = tmp_path / "verify-started"
    verify = f"touch '{mark}'; sleep 0.5; {VERIFY}"

    async def before(who, request):
        if who == "ops" and len(provider.requests["ops"]) == 1:
            for _ in range(1000):
                if mark.exists():
                    return
                await asyncio.sleep(0.01)

    provider = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "bad.txt", "content": "x\n"}, "w1"),
                done("first try"),
                text("done"),
                call("delete", {"file_path": "bad.txt"}, "d1"),
                done("removed it"),
                text("done"),
            ],
            "ops": [
                text("looked around"),
                call("write_file", {"file_path": "c.txt", "content": "c\n"}, "w2"),
                done("wrote c"),
                text("done"),
            ],
        },
        before=before,
    )
    settings = TeamSettings(verify_command=verify, max_attempts=2, max_concurrent=2)
    runner, store, log = make_runner(tmp_path, provider, settings=settings)
    rid = ready_run(
        runner,
        store,
        plan_dict(
            [
                {"id": "T0", "title": "look", "kind": "research", "member": "ops"},
                {"id": "T1", "title": "tidy", "member": "dev"},
                {"id": "T2", "title": "write c", "member": "ops"},
            ],
            members=[
                {"name": "dev", "prompt": "You edit."},
                {"name": "ops", "prompt": "You edit."},
            ],
        ),
    )
    assert await runner.run(rid) == "done"
    assert mark.exists()
    assert [e.data["task"] for e in store.events(rid, kinds=["verify_failed"])] == ["T1"]
    commits = store.task(rid, "T2").commits
    assert commits
    for commit in commits:
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}:bad.txt"], cwd=repo, capture_output=True
        )
        assert probe.returncode != 0
    assert git(repo, "show", f"luna/team/{rid}:c.txt") == "c\n"
    log.close()
    store.close()


async def test_a_failed_member_assembly_leaves_no_worktree(repo, tmp_path):
    def broken_loader(name):
        raise RuntimeError(f"cannot load skill {name}")

    provider = TeamProvider({})
    runner, store, log = make_runner(tmp_path, provider)
    runner.host.skill_names = ("x",)
    runner.host.skill_loader = broken_loader
    rid = ready_run(
        runner,
        store,
        plan_dict(
            [{"id": "T1", "title": "write", "kind": "code", "member": "dev"}],
            members=[{"name": "dev", "prompt": "You write code.", "skills": ["x"]}],
        ),
    )
    assert await runner.run(rid) == "partial"
    task = store.task(rid, "T1")
    assert task.status == "failed" and "cannot load skill x" in task.note
    assert task.worktree is None
    assert list((repo / ".luna" / "worktrees").glob("team-*-T1-*")) == []
    assert "dev" not in provider.requests
    assert store.run(rid).status == "finished"
    assert "**Итог:** ЧАСТИЧНО 0/1" in report_of(repo, rid)
    log.close()
    store.close()
