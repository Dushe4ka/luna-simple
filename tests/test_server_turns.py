import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.server.app import create_app


@pytest.fixture
async def client(tmp_path, monkeypatch, fake_model):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    calls = [AIMessage(content="hello from the agent")]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))
    app = create_app(agent_factory=lambda _workdir: agent, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer secret-token"},
    ) as c:
        yield c, tmp_path


def _parse_sse(raw: str) -> list[dict]:
    events = []
    for block in raw.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


async def test_message_streams_text_delta_and_turn_done(client):
    c, tmp_path = client
    async with c.stream(
        "POST",
        "/sessions/abc/messages",
        json={"content": "hi", "workdir": str(tmp_path)},
    ) as resp:
        assert resp.status_code == 200
        raw = "".join([chunk async for chunk in resp.aiter_text()])
    events = _parse_sse(raw)
    assert {"event": "text_delta", "text": "hello from the agent"} in events
    assert {"event": "turn_done"} == events[-1]


async def test_message_records_session_title_from_first_message(client):
    from luna.core.persistence import SessionIndex

    c, tmp_path = client
    async with c.stream(
        "POST",
        "/sessions/abc/messages",
        json={"content": "please help me fix this bug", "workdir": str(tmp_path)},
    ) as resp:
        async for _ in resp.aiter_text():
            pass
    rows = SessionIndex().list(workdir=str(tmp_path))
    assert len(rows) == 1
    assert rows[0].thread_id == "abc"
    assert rows[0].title == "please help me fix this bug"


async def test_message_that_pauses_on_approval_still_bumps_updated(
    tmp_path, monkeypatch, fake_model
):
    """Regression: a message that triggers an approval pause must still count
    as session activity. ``_stream_turn_events`` must call ``index.touch()``
    unconditionally, not only when a turn completes without interrupting —
    otherwise a session sitting on a pending approval (arguably the one
    needing the most attention) sorts as *less* recently active than it
    actually is, since ``SessionIndex.list()`` orders by ``updated DESC``.
    """
    import time

    from luna.core.persistence import SessionIndex

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / "a.py").write_text("original\n")
    calls = [
        AIMessage(content="ok"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "id": "1",
                    "args": {
                        "file_path": "/a.py",
                        "old_string": "original",
                        "new_string": "changed",
                    },
                }
            ],
        ),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=False)
    agent = build_agent(cfg, model=fake_model(*calls))
    app = create_app(agent_factory=lambda _workdir: agent, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer secret-token"},
    ) as c:
        # First message: completes normally, establishing the session row.
        async with c.stream(
            "POST",
            "/sessions/t3/messages",
            json={"content": "first message", "workdir": str(tmp_path)},
        ) as resp:
            async for _ in resp.aiter_text():
                pass
        first_updated = next(
            r.updated for r in SessionIndex().list(workdir=str(tmp_path)) if r.thread_id == "t3"
        )

        time.sleep(0.01)  # ensure a distinguishable time.time() tick
        before_second_send = time.time()

        # Second message: pauses on an approval interrupt.
        async with c.stream(
            "POST",
            "/sessions/t3/messages",
            json={"content": "edit it", "workdir": str(tmp_path)},
        ) as resp:
            events = _parse_sse("".join([chunk async for chunk in resp.aiter_text()]))
    assert any(e["event"] == "approval_needed" for e in events)

    second_updated = next(
        r.updated for r in SessionIndex().list(workdir=str(tmp_path)) if r.thread_id == "t3"
    )
    assert second_updated >= before_second_send
    assert second_updated > first_updated


def _edit_a_py(tmp_path):
    """A scripted turn that edits a.py, then answers."""
    (tmp_path / "a.py").write_text("original\n")
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "id": "1",
                    "args": {
                        "file_path": "/a.py",
                        "old_string": "original",
                        "new_string": "changed",
                    },
                }
            ],
        ),
        AIMessage(content="done"),
    ]


async def _send_edit(tmp_path, fake_model, thread_id: str) -> list[dict]:
    cfg = LunaConfig(workdir=str(tmp_path), yolo=False)
    agent = build_agent(cfg, model=fake_model(*_edit_a_py(tmp_path)))
    app = create_app(agent_factory=lambda _workdir: agent, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer secret-token"},
    ) as c:
        async with c.stream(
            "POST",
            f"/sessions/{thread_id}/messages",
            json={"content": "edit it", "workdir": str(tmp_path)},
        ) as resp:
            return _parse_sse("".join([chunk async for chunk in resp.aiter_text()]))


async def test_an_allow_rule_auto_approves_without_prompting(tmp_path, monkeypatch, fake_model):
    """Regression (I1): the rule "Always allow" persists must be honoured.

    ``post_approve`` wrote the rule to ``.luna/permissions.toml`` correctly,
    but nothing server-side ever consulted it on a LATER interrupt — so the
    TUI re-prompted for the identical action every single time, even though
    a CLI/REPL session honoured the same file. ``_stream_turn_events`` now
    does what ``collect_decisions`` does.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text('allow = ["edit_file:a.py"]\n')

    events = await _send_edit(tmp_path, fake_model, "allowed")

    assert not any(e["event"] == "approval_needed" for e in events)
    assert {"event": "turn_done"} == events[-1]
    assert (tmp_path / "a.py").read_text() == "changed\n"


async def test_a_deny_rule_auto_rejects_without_prompting(tmp_path, monkeypatch, fake_model):
    """The symmetric case: a denied action never reaches the user either."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text('deny = ["edit_file:a.py"]\n')

    events = await _send_edit(tmp_path, fake_model, "denied")

    assert not any(e["event"] == "approval_needed" for e in events)
    assert {"event": "turn_done"} == events[-1]
    assert (tmp_path / "a.py").read_text() == "original\n"  # untouched


async def test_no_matching_rule_still_asks_the_client(tmp_path, monkeypatch, fake_model):
    """Regression check on the pre-existing behaviour: with no rule that
    matches, the interrupt must still surface as ``approval_needed``."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / ".luna").mkdir()
    rules_file = tmp_path / ".luna" / "permissions.toml"
    rules_file.write_text('allow = ["edit_file:somewhere-else.py"]\n')

    events = await _send_edit(tmp_path, fake_model, "asked")

    assert any(e["event"] == "approval_needed" for e in events)
    assert not any(e["event"] == "turn_done" for e in events)
    assert (tmp_path / "a.py").read_text() == "original\n"
