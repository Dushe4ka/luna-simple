"""Team lifecycle: plan → accept / decline, restart recovery, resume."""

import asyncio
import subprocess
import time
from pathlib import Path

import pytest
from luna_core.errors import LunaCoreError
from luna_core.team.git import TeamGitError
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
    text,
)

ONE = [{"id": "T1", "title": "write b", "kind": "code", "member": "dev"}]


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


async def test_plan_edit_accept_and_decline(repo, tmp_path):
    first = plan_dict([{"id": "T1", "title": "look", "kind": "research", "member": "dev"}])
    second = plan_dict(
        [
            {"id": "T1", "title": "look", "kind": "research", "member": "dev"},
            {"id": "T2", "title": "change", "deps": ["T1"], "member": "dev"},
        ]
    )
    provider = TeamProvider(
        {
            "lead": [
                call("submit_plan", first, "p1"),
                text("planned"),
                call("submit_plan", second, "p2"),
                text("replanned"),
            ]
        }
    )
    runner, store, log = make_runner(tmp_path, provider)
    rid = runner.create("do x")
    plan = await runner.plan(rid)
    assert len(plan.tasks) == 1 and store.run(rid).status == "awaiting_plan"
    assert len(store.events(rid, kinds=["team_plan"])) == 1
    plan = await runner.plan(rid, feedback="split it")
    assert len(plan.tasks) == 2
    assert "split it" in provider.requests["lead"][2].messages[-1].text
    assert provider.requests["lead"][0].model == "lead-model"
    runner.accept(rid, budget=5.0)
    run = store.run(rid)
    assert (run.status, run.budget) == ("running", 5.0)
    assert [t.id for t in store.tasks(rid)] == ["T1", "T2"]
    assert {m.name for m in store.members(rid)} == {"lead", "dev"}
    assert store.member(rid, "lead").steps == 4
    with pytest.raises(LunaCoreError):
        await runner.plan(rid)
    other = runner.create("never mind")
    assert runner.cancel(other) is True
    assert store.run(other).outcome == "stopped"
    assert "**Итог:** ОСТАНОВЛЕНО (отмена)" in report_of(repo, other)
    log.close()
    store.close()


async def test_run_survives_a_restart(repo, tmp_path):
    never = asyncio.Event()

    async def hang(who, request):
        if who == "dev":
            await never.wait()  # the "server" dies while this request is in flight

    first = TeamProvider({"dev": [text("unused")]}, before=hang)
    runner, store, log = make_runner(tmp_path, first)
    rid = ready_run(runner, store, plan_dict(ONE))
    running = asyncio.create_task(runner.run(rid))
    for _ in range(500):
        if first.requests.get("dev"):
            break
        await asyncio.sleep(0.01)
    task = store.task(rid, "T1")
    assert task.status == "in_progress" and Path(task.worktree).exists()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert store.run(rid).status == "running"
    assert store.task(rid, "T1").status == "in_progress"
    log.close()
    store.close()

    second = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
            ]
        }
    )
    runner2, store2, log2 = make_runner(tmp_path, second)
    assert runner2.interrupted() == [rid]
    assert await runner2.run(rid) == "done"
    assert git(repo, "show", f"luna/team/{rid}:b.txt") == "b\n"
    assert "↻ прервано в" in report_of(repo, rid)
    assert "Earlier attempt: прервано в" in second.requests["dev"][0].messages[0].text
    assert store2.task(rid, "T1").attempts == 2
    assert list((repo / ".luna" / "worktrees").glob("team-*")) == []
    assert runner2.interrupted() == []
    log2.close()
    store2.close()


async def test_resume_continues_a_partial_run(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("board_update", {"status": "failed", "note": "need the api key"}, "u1"),
                text("stuck"),
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
            ]
        }
    )
    runner, store, log = make_runner(tmp_path, provider)
    rid = ready_run(runner, store, plan_dict(ONE))
    assert await runner.run(rid) == "partial"
    assert await runner.resume(rid) == "done"
    wakes = [r.messages[-1].text for r in provider.requests["lead"]]
    assert any("Resumed by the user" in w and "ЧАСТИЧНО 0/1" in w for w in wakes)
    task = store.task(rid, "T1")
    assert (task.status, task.attempts) == ("done", 1)
    assert store.events(rid, kinds=["interrupted"]) == []
    assert "**Итог:** ГОТОВО" in report_of(repo, rid)
    with pytest.raises(LunaCoreError):
        await runner.resume(rid + "x")
    log.close()
    store.close()


async def test_a_run_outside_git_fails_with_a_report(tmp_path):
    (tmp_path / "repo").mkdir()
    runner, store, log = make_runner(tmp_path, TeamProvider({}))
    rid = ready_run(runner, store, plan_dict(ONE))
    with pytest.raises(TeamGitError):
        await runner.run(rid)
    run = store.run(rid)
    assert (run.status, run.outcome) == ("finished", "error")
    report = report_of(tmp_path / "repo", rid)
    assert "**Итог:** ОШИБКА" in report and "git repository" in report
    log.close()
    store.close()


async def test_resume_keeps_a_source_blocked_while_its_resolve_task_is_not_done(repo, tmp_path):
    runner, store, log = make_runner(tmp_path, TeamProvider({}))
    rid = ready_run(runner, store, plan_dict(ONE))
    store.add_task(rid, title="resolve", kind="code", merge_of="T1", task_id="T3")
    store.update_task(rid, "T1", status="blocked", branch="luna/team/x/T1")
    store.update_task(rid, "T3", status="failed")
    store.update_run(rid, status="finished", outcome="partial", branch=f"luna/team/{rid}")
    runner._reopen(rid)
    assert store.task(rid, "T3").status == "pending"
    assert store.task(rid, "T1").status == "blocked"
    log.close()
    store.close()


async def test_a_lost_team_branch_ends_the_run_with_an_error_report(repo, tmp_path):
    runner, store, log = make_runner(tmp_path, TeamProvider({}))
    rid = ready_run(runner, store, plan_dict(ONE))
    head = git(repo, "rev-parse", "HEAD").strip()
    # The run started once, then its team branch was deleted by hand.
    store.update_run(rid, branch=f"luna/team/{rid}", base=head, started=time.time())
    with pytest.raises(LunaCoreError):
        await runner.run(rid)
    run = store.run(rid)
    assert (run.status, run.outcome) == ("finished", "error")
    assert "**Итог:** ОШИБКА" in report_of(repo, rid)
    (event,) = store.events(rid, kinds=["team_done"])
    assert event.data["report"].endswith("report.md")
    assert runner.interrupted() == []
    log.close()
    store.close()


async def test_an_early_failure_names_the_report(tmp_path):
    (tmp_path / "repo").mkdir()
    runner, store, log = make_runner(tmp_path, TeamProvider({}))
    rid = ready_run(runner, store, plan_dict(ONE))
    with pytest.raises(TeamGitError):
        await runner.run(rid)
    (event,) = store.events(rid, kinds=["team_done"])
    assert Path(event.data["report"]).is_file()
    log.close()
    store.close()


async def test_an_accepted_run_that_never_started_is_found_and_cancellable(repo, tmp_path):
    runner, store, log = make_runner(tmp_path, TeamProvider({}))
    rid = ready_run(runner, store, plan_dict(ONE))
    log.close()
    store.close()
    # The process died between accept and run.
    runner2, store2, log2 = make_runner(tmp_path, TeamProvider({}))
    assert runner2.interrupted() == [rid]
    assert runner2.cancel(rid) is True
    run = store2.run(rid)
    assert (run.status, run.outcome, run.stop_reason) == ("finished", "stopped", "cancel")
    assert "**Итог:** ОСТАНОВЛЕНО (отмена)" in report_of(repo, rid)
    assert runner2.interrupted() == []
    assert runner2.cancel(rid) is False
    log2.close()
    store2.close()


async def test_an_interrupted_run_can_be_cancelled_after_a_restart(repo, tmp_path):
    never = asyncio.Event()

    async def hang(who, request):
        if who == "dev":
            await never.wait()

    first = TeamProvider(
        {"dev": [call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1")]},
        before=hang,
    )
    runner, store, log = make_runner(tmp_path, first)
    rid = ready_run(runner, store, plan_dict(ONE))
    running = asyncio.create_task(runner.run(rid))
    for _ in range(500):
        if first.requests.get("dev"):
            break
        await asyncio.sleep(0.01)
    worktree = Path(store.task(rid, "T1").worktree)
    (worktree / "b.txt").write_text("half done\n")
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    log.close()
    store.close()

    runner2, store2, log2 = make_runner(tmp_path, TeamProvider({}))
    assert runner2.interrupted() == [rid]
    assert runner2.cancel(rid) is True
    run = store2.run(rid)
    assert (run.status, run.outcome, run.stop_reason) == ("finished", "stopped", "cancel")
    task = store2.task(rid, "T1")
    assert task.status == "pending" and task.worktree is None and task.branch
    assert git(repo, "show", f"{task.branch}:b.txt") == "half done\n"
    assert list((repo / ".luna" / "worktrees").glob("team-*")) == []
    assert "**Итог:** ОСТАНОВЛЕНО (отмена)" in report_of(repo, rid)
    log2.close()
    store2.close()


async def test_a_restart_during_verify_rolls_the_unverified_merge_back(repo, tmp_path):
    started, ended, verified = tmp_path / "started", tmp_path / "ended", tmp_path / "verified"
    slow = f'touch "{started}" && sleep 1 && touch "{ended}"'
    first = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
            ]
        }
    )
    runner, store, log = make_runner(tmp_path, first, settings=TeamSettings(verify_command=slow))
    rid = ready_run(runner, store, plan_dict(ONE))
    running = asyncio.create_task(runner.run(rid))
    for _ in range(1000):
        if started.exists():
            break
        await asyncio.sleep(0.01)
    assert started.exists()
    # The merge is on the team branch but not verified yet: the process dies now.
    assert git(repo, "show", f"luna/team/{rid}:b.txt") == "b\n"
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    for _ in range(500):
        if ended.exists():
            break
        await asyncio.sleep(0.01)
    log.close()
    store.close()

    seen: list[bool] = []

    async def look(who, request):
        if who == "dev" and not seen:
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"luna/team/{rid}:b.txt"], cwd=repo, check=False
            )
            seen.append(probe.returncode == 0)

    second = TeamProvider({"dev": [done("b is there"), text("done")]}, before=look)
    check = f'echo v >> "{verified}" && test -f b.txt'
    runner2, store2, log2 = make_runner(
        tmp_path, second, settings=TeamSettings(verify_command=check)
    )
    assert runner2.interrupted() == [rid]
    assert await runner2.run(rid) == "done"
    assert seen == [False]  # the unverified merge was rolled back before the retry
    assert verified.read_text().count("v") == 1  # and the retry was verified
    assert git(repo, "show", f"luna/team/{rid}:b.txt") == "b\n"
    assert store2.task(rid, "T1").status == "done"
    assert len(store2.events(rid, kinds=["merge_undone"])) == 1
    log2.close()
    store2.close()


async def test_cancel_after_a_restart_during_verify_rolls_the_merge_back(repo, tmp_path):
    started, ended = tmp_path / "started", tmp_path / "ended"
    slow = f'touch "{started}" && sleep 1 && touch "{ended}"'
    first = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
            ]
        }
    )
    runner, store, log = make_runner(tmp_path, first, settings=TeamSettings(verify_command=slow))
    rid = ready_run(runner, store, plan_dict(ONE))
    running = asyncio.create_task(runner.run(rid))
    for _ in range(1000):
        if started.exists():
            break
        await asyncio.sleep(0.01)
    assert started.exists()
    assert git(repo, "show", f"luna/team/{rid}:b.txt") == "b\n"
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    for _ in range(500):
        if ended.exists():
            break
        await asyncio.sleep(0.01)
    log.close()
    store.close()

    runner2, store2, log2 = make_runner(tmp_path, TeamProvider({}))
    assert runner2.interrupted() == [rid]
    assert runner2.cancel(rid) is True
    run = store2.run(rid)
    assert (run.status, run.outcome, run.stop_reason) == ("finished", "stopped", "cancel")
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"luna/team/{rid}:b.txt"], cwd=repo, check=False
    )
    assert probe.returncode != 0  # the unverified merge is not on the team branch
    assert len(store2.events(rid, kinds=["merge_undone"])) == 1
    assert report_of(repo, rid)
    log2.close()
    store2.close()


async def test_the_planner_obeys_deny_rules(repo, tmp_path):
    from luna_core.policy import Rules
    from team_kit import result_of

    (repo / ".env").write_text("SECRET=1\n")
    provider = TeamProvider(
        {
            "lead": [
                call("read_file", {"file_path": ".env"}, "r1"),
                call("submit_plan", plan_dict(ONE), "p1"),
                text("planned"),
            ]
        }
    )
    runner, store, log = make_runner(tmp_path, provider, base_rules=Rules(deny=["read_file:.env"]))
    rid = runner.create("plan it")
    await runner.plan(rid)
    found = result_of(provider.requests["lead"][1], "r1")
    assert found is not None and found.is_error
    log.close()
    store.close()
