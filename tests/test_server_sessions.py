from datetime import datetime

import httpx
import pytest

from luna.core.persistence import SessionIndex
from luna.server.app import create_app
from luna.server.sessions import relative_time, session_group


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    app = create_app(agent_factory=lambda _workdir: None, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


async def test_create_session_returns_a_thread_id(client):
    resp = await client.post("/sessions", json={"workdir": "/repo"})
    assert resp.status_code == 200
    data = resp.json()
    assert "thread_id" in data and len(data["thread_id"]) > 0


async def test_list_sessions_newest_first(client, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    index = SessionIndex()
    index.record("t1", "/repo", "first session")
    index.record("t2", "/repo", "second session")
    index.touch("t2")
    resp = await client.get("/sessions", params={"workdir": "/repo"})
    assert resp.status_code == 200
    rows = resp.json()["sessions"]
    assert [r["thread_id"] for r in rows] == ["t2", "t1"]
    assert rows[0]["title"] == "second session"
    assert "relative_time" in rows[0]


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


async def test_list_sessions_includes_group(client, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    SessionIndex().record("t1", "/repo", "first session")
    rows = (await client.get("/sessions", params={"workdir": "/repo"})).json()["sessions"]
    assert rows[0]["group"] == "today"


async def test_list_sessions_returns_more_than_twenty(client, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    index = SessionIndex()
    for i in range(25):
        index.record(f"t{i}", "/repo", f"session {i}")
    rows = (await client.get("/sessions", params={"workdir": "/repo"})).json()["sessions"]
    assert len(rows) == 25
