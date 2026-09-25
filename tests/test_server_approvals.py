import json

import httpx
from langchain_core.messages import AIMessage

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.server.app import create_app


def _parse_sse(raw: str) -> list[dict]:
    events = []
    for block in raw.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


async def test_approve_with_always_persists_a_project_rule(tmp_path, monkeypatch, fake_model):
    from luna.core import permissions

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / "a.py").write_text("original\n")
    calls = [
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
    cfg = LunaConfig(workdir=str(tmp_path), yolo=False)
    agent = build_agent(cfg, model=fake_model(*calls))
    app = create_app(agent_factory=lambda _workdir: agent, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer secret-token"},
    ) as c:
        async with c.stream(
            "POST",
            "/sessions/t2/messages",
            json={"content": "edit it", "workdir": str(tmp_path)},
        ) as resp:
            async for _ in resp.aiter_text():
                pass
        async with c.stream(
            "POST",
            "/sessions/t2/approve",
            json={
                "decision": {"type": "approve", "always": "edit_file:a.py"},
                "workdir": str(tmp_path),
            },
        ) as resp:
            events = _parse_sse("".join([chunk async for chunk in resp.aiter_text()]))
    assert (tmp_path / "a.py").read_text() == "changed\n"
    rules = permissions.load_rules(str(tmp_path))
    assert "edit_file:a.py" in rules.allow
    # the graph's resume payload must never see the "always" key
    assert {"event": "turn_done"} == events[-1]


async def test_always_allow_is_honoured_on_the_next_identical_action(
    tmp_path, monkeypatch, fake_model
):
    """Regression (I1): the whole point of the modal's "Always allow" button.

    The rule was persisted correctly, but nothing server-side consulted it on
    a later interrupt, so the TUI re-prompted for the identical action on
    every occurrence — a user-facing no-op. The second message below must go
    straight through with no ``approval_needed`` event at all.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / "a.py").write_text("original\n")

    def edit(old: str, new: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "edit_file",
                    "id": "1",
                    "args": {"file_path": "/a.py", "old_string": old, "new_string": new},
                }
            ],
        )

    calls = [
        edit("original", "changed"),
        AIMessage(content="done"),
        edit("changed", "again"),
        AIMessage(content="done again"),
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
        async with c.stream(
            "POST",
            "/sessions/t9/messages",
            json={"content": "edit it", "workdir": str(tmp_path)},
        ) as resp:
            first = _parse_sse("".join([chunk async for chunk in resp.aiter_text()]))
        assert any(e["event"] == "approval_needed" for e in first)

        async with c.stream(
            "POST",
            "/sessions/t9/approve",
            json={
                "decision": {"type": "approve", "always": "edit_file:a.py"},
                "workdir": str(tmp_path),
            },
        ) as resp:
            async for _ in resp.aiter_text():
                pass
        assert (tmp_path / "a.py").read_text() == "changed\n"

        # Same action again, in a new message: the persisted rule must apply.
        async with c.stream(
            "POST",
            "/sessions/t9/messages",
            json={"content": "edit it again", "workdir": str(tmp_path)},
        ) as resp:
            second = _parse_sse("".join([chunk async for chunk in resp.aiter_text()]))

    assert not any(e["event"] == "approval_needed" for e in second)
    assert {"event": "turn_done"} == second[-1]
    assert (tmp_path / "a.py").read_text() == "again\n"


async def test_approve_resumes_a_blocked_edit(tmp_path, monkeypatch, fake_model):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    (tmp_path / "a.py").write_text("original\n")
    calls = [
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
    cfg = LunaConfig(workdir=str(tmp_path), yolo=False)
    agent = build_agent(cfg, model=fake_model(*calls))
    app = create_app(agent_factory=lambda _workdir: agent, token="secret-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer secret-token"},
    ) as c:
        async with c.stream(
            "POST",
            "/sessions/t1/messages",
            json={"content": "edit it", "workdir": str(tmp_path)},
        ) as resp:
            first_pass = _parse_sse("".join([chunk async for chunk in resp.aiter_text()]))
        assert any(e["event"] == "approval_needed" for e in first_pass)
        assert (tmp_path / "a.py").read_text() == "original\n"

        async with c.stream(
            "POST",
            "/sessions/t1/approve",
            json={"decision": {"type": "approve"}, "workdir": str(tmp_path)},
        ) as resp:
            second_pass = _parse_sse("".join([chunk async for chunk in resp.aiter_text()]))
    assert (tmp_path / "a.py").read_text() == "changed\n"
    assert {"event": "turn_done"} == second_pass[-1]
