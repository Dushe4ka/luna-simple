"""The team board store, settings and mailbox."""

import threading

import pytest
from luna_core.team.board import LEAD, TeamStore
from luna_core.team.messages import Mailbox
from luna_core.team.settings import TeamSettings


@pytest.fixture
def store(tmp_path):
    s = TeamStore(tmp_path / "s.db")
    yield s
    s.close()


def new_run(store: TeamStore) -> str:
    return store.create_run(
        workdir="/w", task="do it", session_id="sess", settings=TeamSettings().to_dict()
    )


def test_settings_validate_and_roundtrip():
    with pytest.raises(ValueError):
        TeamSettings(max_concurrent=0)
    with pytest.raises(ValueError):
        TeamSettings(rpm=0)
    with pytest.raises(ValueError):
        TeamSettings(budget=-1)
    settings = TeamSettings(models=("x",), budget=2.0)
    assert TeamSettings.from_dict(settings.to_dict()) == settings


def test_run_roundtrip(store):
    rid = new_run(store)
    run = store.run(rid)
    assert (run.status, run.task, run.settings["max_concurrent"]) == ("planning", "do it", 4)
    store.update_run(rid, status="running", plan={"goal": "g"}, branch="luna/team/x")
    run = store.run(rid)
    assert (run.status, run.plan, run.branch) == ("running", {"goal": "g"}, "luna/team/x")
    assert [r.id for r in store.runs(status="running")] == [rid]
    with pytest.raises(ValueError):
        store.update_run(rid, status="weird")
    with pytest.raises(ValueError):
        store.update_run(rid, nonsense=1)


def test_tasks_get_ids_and_only_ready_ones_are_claimed(store):
    rid = new_run(store)
    t1 = store.add_task(rid, title="a", kind="code")
    t2 = store.add_task(rid, title="b", kind="research", deps=[t1.id])
    assert (t1.id, t2.id) == ("T1", "T2")
    assert [t.id for t in store.ready(rid)] == ["T1"]
    got = store.claim_next(rid, "dev")
    assert (got.id, got.status, got.assignee, got.attempts) == ("T1", "in_progress", "dev", 1)
    assert store.claim_next(rid, "dev2") is None
    store.update_task(rid, "T1", status="done")
    assert store.claim_next(rid, "dev2").id == "T2"


def test_unknown_dependency_kind_and_duplicate_id_are_refused(store):
    rid = new_run(store)
    with pytest.raises(ValueError, match="unknown dependencies"):
        store.add_task(rid, title="x", deps=["T9"])
    with pytest.raises(ValueError, match="kind"):
        store.add_task(rid, title="x", kind="poem")
    store.add_task(rid, title="x", task_id="A")
    with pytest.raises(ValueError, match="already exists"):
        store.add_task(rid, title="y", task_id="A")
    assert store.add_task(rid, title="z").id == "T1"


def test_assigned_tasks_go_to_their_member_first(store):
    rid = new_run(store)
    store.add_task(rid, title="free")
    store.add_task(rid, title="mine", assignee="bob")
    assert store.claim_next(rid, "bob").title == "mine"
    assert store.claim_next(rid, "amy").title == "free"
    store.add_task(rid, title="bobs", assignee="bob")
    assert store.claim_next(rid, "amy") is None


def test_claim_is_atomic_across_connections(tmp_path):
    a, b = TeamStore(tmp_path / "s.db"), TeamStore(tmp_path / "s.db")
    rid = new_run(a)
    a.add_task(rid, title="only")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def grab(store: TeamStore, who: str) -> None:
        barrier.wait()
        results.append(store.claim(rid, "T1", who))

    threads = [threading.Thread(target=grab, args=(s, n)) for s, n in ((a, "x"), (b, "y"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    a.close()
    b.close()


def test_remove_task_rewires_dependents(store):
    rid = new_run(store)
    store.add_task(rid, title="a")
    store.add_task(rid, title="b", deps=["T1"])
    store.add_task(rid, title="c")
    store.remove_task(rid, "T1", replace_with=["T3"])
    assert store.task(rid, "T1") is None and store.task(rid, "T2").deps == ("T3",)


def test_members_events_and_fingerprint(store):
    rid = new_run(store)
    store.add_task(rid, title="a")
    store.add_member(rid, "dev", {"prompt": "p"})
    with pytest.raises(ValueError, match="already"):
        store.add_member(rid, "dev", {})
    seen = []
    unsubscribe = store.subscribe(seen.append)
    before = store.fingerprint(rid)
    store.log_event(rid, "team_message", "dev", to="lead", text="hi")
    assert store.fingerprint(rid) == before  # chatter does not move the board
    store.claim_next(rid, "dev")
    assert store.fingerprint(rid) != before
    store.update_member(rid, "dev", add_steps=3, add_cost=0.5, add_busy=2.0)
    member = store.member(rid, "dev")
    assert (member.steps, member.cost, member.busy, member.spec) == (3, 0.5, 2.0, {"prompt": "p"})
    unsubscribe()
    store.log_event(rid, "team_task", task="T1")
    assert [e.kind for e in seen] == ["team_message"]
    assert [e.data for e in store.events(rid, kinds=["team_task"])] == [{"task": "T1"}]


def test_mailbox_direct_and_broadcast(store):
    rid = new_run(store)
    box = Mailbox(store)
    store.add_member(rid, LEAD, {"role": "lead"})
    for name in ("amy", "bob"):
        store.add_member(rid, name, {})
    box.send(rid, "amy", "bob", "need T1 api")
    with pytest.raises(ValueError):
        box.send(rid, "amy", "*", "all")
    with pytest.raises(ValueError):
        box.send(rid, "amy", "nobody", "x")
    with pytest.raises(ValueError):
        box.send(rid, "amy", "amy", "me")
    with pytest.raises(ValueError, match="only the lead"):
        box.broadcast(rid, "amy", "everyone")
    assert box.broadcast(rid, LEAD, "stop and commit") == 2
    assert [m.text for m in box.unread(rid, "bob")] == ["need T1 api", "stop and commit"]
    assert box.unread(rid, "bob") == []
    box.send(rid, "bob", LEAD, "done")
    assert box.count_unread(rid, LEAD) == 1
    assert [e.data["to"] for e in store.events(rid, kinds=["team_message"])] == [
        "bob",
        "*",
        "lead",
    ]


def test_delete_run_forgets_everything(store):
    rid = new_run(store)
    store.add_task(rid, title="a")
    store.add_member(rid, "dev", {})
    store.log_event(rid, "team_task")
    store.delete_run(rid)
    assert store.run(rid) is None and store.tasks(rid) == [] and store.events(rid) == []
