from datetime import datetime

import pytest

from luna.server.sessions import relative_time, session_group
from tests.server_helpers import client, make_app, seed


def _age(log, session_id: str, updated: float) -> None:
    with log._lock:
        log._conn.execute("UPDATE sessions SET updated = ? WHERE id = ?", (updated, session_id))


async def test_create_session_returns_an_id(tmp_path):
    app = make_app()
    async with client(app) as c:
        resp = await c.post("/sessions", json={"workdir": str(tmp_path)})
    assert resp.status_code == 200
    data = resp.json()
    assert list(data) == ["id"] and data["id"]
    assert app.state.log.info(data["id"]).workdir == str(tmp_path.resolve())


async def test_list_sessions_newest_first(tmp_path):
    import time

    app = make_app()
    log = app.state.log
    seed(log, tmp_path, "first session", session_id="t1")
    seed(log, tmp_path, "second session", session_id="t2")
    _age(log, "t1", time.time() - 60)
    async with client(app) as c:
        resp = await c.get("/sessions", params={"workdir": str(tmp_path)})
    rows = resp.json()["sessions"]
    assert [r["id"] for r in rows] == ["t2", "t1"]
    assert rows[0]["title"] == "second session"
    assert rows[0]["relative_time"] == "сейчас" and rows[1]["relative_time"] == "1м"
    assert rows[0]["running"] is False and rows[0]["background"] is False


NOW = datetime(2026, 9, 28, 12, 0).timestamp()


def _ts(*args) -> float:
    return datetime(*args).timestamp()


@pytest.mark.parametrize(
    ("updated", "group", "label"),
    [
        (NOW - 30, "today", "сейчас"),
        (NOW - 11 * 60, "today", "11м"),
        (NOW - 3 * 3600, "today", "3ч"),
        (_ts(2026, 9, 27, 23, 59), "yesterday", "23:59"),
        (_ts(2026, 9, 22, 10, 0), "week", "вт"),
        (_ts(2026, 9, 21, 10, 0), "older", "21 сен"),
        (_ts(2026, 9, 20, 10, 0), "older", "20 сен"),
        (_ts(2025, 12, 31, 10, 0), "older", "31 дек"),
    ],
)
def test_group_and_label(updated, group, label):
    assert session_group(updated, now=NOW) == group
    assert relative_time(updated, now=NOW) == label


def test_calendar_day_boundary_not_24_hours():
    just_after_midnight = _ts(2026, 9, 28, 0, 1)
    two_minutes_earlier = _ts(2026, 9, 27, 23, 59)
    assert session_group(two_minutes_earlier, now=just_after_midnight) == "yesterday"


def test_future_timestamp_is_treated_as_now():
    assert session_group(NOW + 600, now=NOW) == "today"
    assert relative_time(NOW + 600, now=NOW) == "сейчас"


async def test_list_sessions_includes_group(tmp_path):
    app = make_app()
    seed(app.state.log, tmp_path, "first session")
    async with client(app) as c:
        rows = (await c.get("/sessions", params={"workdir": str(tmp_path)})).json()["sessions"]
    assert rows[0]["group"] == "today"


async def test_list_sessions_returns_more_than_twenty(tmp_path):
    app = make_app()
    for i in range(25):
        seed(app.state.log, tmp_path, f"session {i}")
    async with client(app) as c:
        rows = (await c.get("/sessions", params={"workdir": str(tmp_path)})).json()["sessions"]
    assert len(rows) == 25


async def test_other_projects_sessions_are_not_listed(tmp_path):
    app = make_app()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    seed(app.state.log, tmp_path / "a", "mine")
    seed(app.state.log, tmp_path / "b", "theirs")
    async with client(app) as c:
        rows = (await c.get("/sessions", params={"workdir": str(tmp_path / "a")})).json()
    assert [r["title"] for r in rows["sessions"]] == ["mine"]


async def test_a_running_session_cannot_be_deleted(tmp_path):
    from luna_core.providers.fake import FakeProvider

    from tests.server_helpers import turn

    (tmp_path / "a.py").write_text("x\n")
    app = make_app(default=[FakeProvider.call("delete", {"file_path": "a.py"}, "d1")])
    async with client(app) as c:
        _, events = await turn(c, "t1", tmp_path, "delete it")
        assert events[-1]["event"] == "approval_needed"
        resp = await c.delete("/sessions/t1")
    assert resp.status_code == 409 and resp.json() == {"error": "session_busy"}
    assert (tmp_path / "a.py").exists()


async def test_a_fork_is_listed(tmp_path):
    from luna_core.session import Session

    app = make_app()
    log = app.state.log
    parent = seed(log, tmp_path, "work", session_id="t1")
    fork = Session(log, parent, workdir=str(tmp_path.resolve()), undo=False).fork()
    async with client(app) as c:
        rows = (await c.get("/sessions", params={"workdir": str(tmp_path)})).json()["sessions"]
    assert {r["id"] for r in rows} == {parent, fork.id}
