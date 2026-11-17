"""Assembly journal: runs, profile versions, notices, meta."""

import threading

import pytest
from luna_core.assemblies import TASK_HEAD, AssemblyLog


@pytest.fixture
def journal(tmp_path):
    j = AssemblyLog(tmp_path / "db" / "s.db")
    yield j
    j.close()


def _rec(journal, **kw):
    base = dict(
        workdir="/w",
        parent_session="p1",
        child_session="c1",
        base_profile="general-purpose",
        dynamic=True,
        spec={"name": "general-purpose", "prompt": "Write migrations", "tools": ["read_file"]},
        task="add a column",
        ok=True,
        cancelled=False,
        hit_max_steps=False,
        steps=3,
        cost=0.01,
    )
    base.update(kw)
    return journal.record(**base)


def test_record_roundtrip_and_task_head(journal):
    rec = _rec(journal, task="x" * 500, mutating=("mcp__db__write",))
    got = journal.get(rec.id)
    assert got == rec
    assert len(got.task_head) == TASK_HEAD
    assert got.mutating == ("mcp__db__write",)
    assert got.spec["prompt"] == "Write migrations"
    assert got.profile_name is None


@pytest.mark.parametrize(
    ("ok", "cancelled", "hit", "success"),
    [
        (True, False, False, True),
        (False, False, False, False),
        (True, True, False, False),
        (True, False, True, False),
    ],
)
def test_success_rule(journal, ok, cancelled, hit, success):
    assert _rec(journal, ok=ok, cancelled=cancelled, hit_max_steps=hit).success is success


def test_pending_newest_limit_in_id_order_and_mark(journal):
    ids = [_rec(journal).id for _ in range(5)]
    assert [r.id for r in journal.pending(limit=3)] == ids[2:]
    journal.mark(ids[3:], "migration-writer")
    assert [r.id for r in journal.pending()] == ids[:3]
    assert journal.get(ids[4]).profile_name == "migration-writer"
    assert journal.last_id() == ids[-1]
    assert journal.count_after(ids[1]) == 3


def test_recent_for_profile_newest_first(journal):
    a = _rec(journal, base_profile="x").id
    _rec(journal, base_profile="y")
    b = _rec(journal, base_profile="x").id
    assert [r.id for r in journal.recent_for("x")] == [b, a]


def test_versions_and_undone(journal):
    v1 = journal.add_version(
        name="m", path="/u.toml", action="save", version=1, spec={"prompt": "a"}, prev=None
    )
    v2 = journal.add_version(
        name="m",
        path="/u.toml",
        action="update",
        version=2,
        spec={"prompt": "b"},
        prev={"prompt": "a"},
    )
    journal.add_version(name="other", path="/u.toml", action="save", version=1, spec={}, prev=None)
    assert [v.id for v in journal.versions("m")] == [v2.id, v1.id]
    journal.set_undone(v2.id)
    assert journal.versions("m")[0].undone is True
    assert len(journal.versions()) == 3


def test_notices_drain_once(journal):
    journal.push_notice("info", "one")
    journal.push_notice("warn", "two")
    assert journal.drain_notices() == [("info", "one"), ("warn", "two")]
    assert journal.drain_notices() == []


def test_meta_and_claim(journal):
    assert journal.get_meta("k") is None
    assert journal.claim_meta("k", expect=None, value="1") is True
    assert journal.claim_meta("k", expect=None, value="2") is False
    assert journal.claim_meta("k", expect="1", value="2") is True
    assert journal.get_meta("k") == "2"


def test_claim_is_atomic_across_connections(tmp_path):
    path = tmp_path / "s.db"
    logs = [AssemblyLog(path) for _ in range(4)]
    wins = []

    def go(j):
        wins.append(j.claim_meta("lock", expect=None, value="x"))

    threads = [threading.Thread(target=go, args=(j,)) for j in logs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1
    for j in logs:
        j.close()


def test_shares_file_with_event_log(tmp_path):
    from luna_core.session import EventLog

    path = tmp_path / "s.db"
    log = EventLog(path)
    j = AssemblyLog(path)
    sid = log.create("/w")
    _rec(j)
    assert log.info(sid) is not None and j.last_id() == 1
    j.close()
    log.close()
