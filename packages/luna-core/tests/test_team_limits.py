"""Team limits: concurrency, steps, members, budget, wall clock, cancel, stall, sleep."""

import contextlib

import pytest
from luna_core.errors import LunaCoreError
from luna_core.session import Session
from luna_core.team.git import TeamGit
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

TWO_TASKS = [
    {"id": "T1", "title": "first", "member": "dev"},
    {"id": "T2", "title": "second", "deps": ["T1"], "member": "dev"},
]


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


async def test_no_more_than_max_concurrent_members_work_at_once(repo, tmp_path):
    names = ["amy", "bob", "cat"]
    provider = TeamProvider(
        {
            n: [
                call("write_file", {"file_path": f"{n}.txt", "content": n}, "w"),
                done(n),
                text("ok"),
            ]
            for n in names
        },
        delay=0.05,
    )
    runner, store, log = make_runner(tmp_path, provider, settings=TeamSettings(max_concurrent=2))
    rid = ready_run(
        runner,
        store,
        plan_dict(
            [{"id": f"T{i}", "title": f"file {n}", "member": n} for i, n in enumerate(names, 1)],
            members=[{"name": n, "prompt": "You write."} for n in names],
        ),
    )
    assert await runner.run(rid) == "done"
    busy = peak = 0
    for event in store.events(rid, kinds=["team_member"]):
        if event.data.get("state") == "working":
            busy += 1
            peak = max(peak, busy)
        elif event.data.get("state") == "idle":
            busy -= 1
    assert peak == 2
    log.close()
    store.close()


async def test_member_steps_are_capped(repo, tmp_path):
    reads = [call("read_file", {"file_path": "a.txt"}, f"r{i}") for i in range(5)]
    provider = TeamProvider({"dev": reads})
    runner, store, log = make_runner(tmp_path, provider, settings=TeamSettings(member_steps=2))
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "read", "member": "dev"}]))
    assert await runner.run(rid) == "partial"
    task = store.task(rid, "T1")
    assert (task.status, task.note) == ("failed", "ran out of steps (2)")
    assert len(provider.requests["dev"]) == 2
    log.close()
    store.close()


async def test_the_lead_goes_quiet_when_its_steps_run_out(repo, tmp_path):
    provider = TeamProvider({"dev": [done("one"), text("ok"), done("two"), text("ok")]})
    runner, store, log = make_runner(tmp_path, provider, settings=TeamSettings(lead_steps=1))
    rid = ready_run(runner, store, plan_dict(TWO_TASKS))
    assert await runner.run(rid) == "done"
    assert len(provider.requests["lead"]) == 1
    assert len(store.events(rid, kinds=["lead_out_of_steps"])) == 1
    log.close()
    store.close()


def test_hire_respects_the_member_limit_and_checks_the_assembly(repo, tmp_path):
    runner, store, log = make_runner(
        tmp_path, TeamProvider({}), settings=TeamSettings(max_members=2)
    )
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "x", "member": "dev"}]))
    with pytest.raises(LunaCoreError, match="unknown tools"):
        runner._hire(rid, {"name": "qa", "prompt": "test", "tools": ["nuke"]})
    assert runner._hire(rid, {"name": "qa", "prompt": "test"}) == "hired qa"
    with pytest.raises(ValueError, match="member limit"):
        runner._hire(rid, {"name": "qb", "prompt": "test"})
    log.close()
    store.close()


async def test_budget_stops_softly(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
            ]
        }
    )
    runner, store, log = make_runner(
        tmp_path,
        provider,
        settings=TeamSettings(budget=20.0, models=("member-model",)),
        overrides={"member-model": {"input": 1_000_000, "output": 1_000_000}},
    )
    rid = ready_run(
        runner,
        store,
        plan_dict(
            TWO_TASKS, members=[{"name": "dev", "prompt": "You write.", "model": "member-model"}]
        ),
    )
    assert await runner.run(rid) == "stopped"
    assert store.run(rid).stop_reason == "budget"
    assert store.task(rid, "T1").status == "done" and store.task(rid, "T2").status == "pending"
    assert git(repo, "show", f"luna/team/{rid}:b.txt") == "b\n"
    assert len(provider.requests["dev"]) == 2  # the third call was refused by max_cost
    assert store.member(rid, "dev").cost == pytest.approx(30.0)
    assert "**Итог:** ОСТАНОВЛЕНО (бюджет)" in report_of(repo, rid)
    log.close()
    store.close()


async def test_wall_clock_stops_softly_and_grants_one_more_step(repo, tmp_path):
    clock = Clock()

    async def jump(who, request):
        if who == "dev" and len(provider.requests["dev"]) == 1:
            clock.now += 10_000

    writes = [
        call("write_file", {"file_path": f"f{i}.txt", "content": "x"}, f"w{i}") for i in range(1, 5)
    ]
    provider = TeamProvider({"dev": writes}, delay=0.2, before=jump)
    runner, store, log = make_runner(
        tmp_path, provider, settings=TeamSettings(wall_clock=100), clock=clock
    )
    rid = ready_run(runner, store, plan_dict(TWO_TASKS))
    assert await runner.run(rid) == "stopped"
    assert store.run(rid).stop_reason == "time"
    assert len(provider.requests["dev"]) == 2
    first = store.task(rid, "T1")
    assert first.status == "pending" and first.note.startswith("stopped (time)") and first.branch
    assert git(repo, "show", f"{first.branch}:f2.txt") == "x"
    assert store.task(rid, "T2").status == "pending"
    assert "**Итог:** ОСТАНОВЛЕНО (время)" in report_of(repo, rid)
    log.close()
    store.close()


async def test_cancel_stops_softly(repo, tmp_path):
    holder = {}

    async def cancel_on_first(who, request):
        if who == "dev" and len(provider.requests["dev"]) == 1:
            assert holder["runner"].cancel(holder["rid"]) is True

    provider = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
            ]
        },
        before=cancel_on_first,
    )
    runner, store, log = make_runner(tmp_path, provider)
    rid = ready_run(runner, store, plan_dict(TWO_TASKS))
    holder.update(runner=runner, rid=rid)
    assert await runner.run(rid) == "stopped"
    assert store.run(rid).stop_reason == "cancel"
    assert store.task(rid, "T1").status == "done" and store.task(rid, "T2").status == "pending"
    assert runner.cancel(rid) is False  # finished runs cannot be cancelled
    log.close()
    store.close()


def test_stall_detector_warns_then_stops(repo, tmp_path):
    runner, store, log = make_runner(tmp_path, TeamProvider({}))
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "x", "member": "dev"}]))
    run = store.run(rid)
    st = runner._new_state(run, TeamGit(repo, rid), Session.open(log, run.session_id, undo=False))
    assert [runner._stall_check(st) for _ in range(4)] == ["", "", "", "warn"]
    store.update_task(rid, "T1", note="still thinking")  # notes do not count as progress
    assert runner._stall_check(st) == ""  # idle 4: past the warning, not yet the stop
    store.update_task(rid, "T1", status="blocked")  # a real change resets the count
    assert [runner._stall_check(st) for _ in range(7)] == ["", "", "", "warn", "", "", "stop"]
    assert st.stalled and st.stopping == "stall"
    assert runner._settled(st) == "stalled"
    kinds = [e.kind for e in store.events(rid, kinds=["stall_warn", "stalled", "soft_stop"])]
    assert kinds == ["stall_warn", "stall_warn", "stalled", "soft_stop"]
    log.close()
    store.close()


@pytest.mark.parametrize(("allow", "held"), [(False, True), (True, False)])
async def test_sleep_is_held_off_unless_allowed(tmp_path, allow, held):
    make_repo(tmp_path / "repo")
    seen = []

    @contextlib.contextmanager
    def fake(enabled):
        seen.append(enabled)
        yield enabled

    provider = TeamProvider({"dev": [done("ok"), text("ok")]})
    runner, store, log = make_runner(
        tmp_path, provider, settings=TeamSettings(allow_sleep=allow), keep_awake=fake
    )
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "x", "member": "dev"}]))
    await runner.run(rid)
    assert seen == [held]
    assert store.events(rid, kinds=["awake"])[0].data == {"held": held}
    log.close()
    store.close()
