"""Team lifecycle: plan → accept / decline, restart recovery, resume."""

import asyncio
from pathlib import Path

import pytest
from luna_core.errors import LunaCoreError
from luna_core.team.git import TeamGitError
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
