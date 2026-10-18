from luna_core.policy import Rules
from luna_core.providers.fake import FakeProvider

from tests.server_helpers import client, make_app, turn


def _edit(old: str, new: str, call_id: str = "e1"):
    args = {"file_path": "a.py", "old_string": old, "new_string": new}
    return FakeProvider.call("edit_file", args, call_id)


async def test_approve_with_always_persists_a_project_rule(tmp_path):
    (tmp_path / "a.py").write_text("original\n")
    app = make_app(default=[_edit("original", "changed"), FakeProvider.text("done")])
    async with client(app) as c:
        _, events = await turn(
            c, "t2", tmp_path, "edit it", decide={"type": "approve", "always": "edit_file:a.py"}
        )
    assert (tmp_path / "a.py").read_text() == "changed\n"
    rules = Rules.load([tmp_path / ".luna" / "permissions.toml"])
    assert "edit_file:a.py" in rules.allow
    assert events[-1]["event"] == "turn_done"


async def test_always_allow_is_honoured_on_the_next_identical_action(tmp_path):
    """The whole point of the modal's "Always allow" button: no second prompt."""
    (tmp_path / "a.py").write_text("original\n")
    app = make_app(
        default=[
            _edit("original", "changed"),
            FakeProvider.text("done"),
            _edit("changed", "again", "e2"),
            FakeProvider.text("done again"),
        ]
    )
    async with client(app) as c:
        _, first = await turn(
            c, "t9", tmp_path, "edit it", decide={"type": "approve", "always": "edit_file:a.py"}
        )
        assert any(e["event"] == "approval_needed" for e in first)
        assert (tmp_path / "a.py").read_text() == "changed\n"
        _, second = await turn(c, "t9", tmp_path, "edit it again")
    assert not any(e["event"] == "approval_needed" for e in second)
    assert second[-1]["event"] == "turn_done"
    assert (tmp_path / "a.py").read_text() == "again\n"


async def test_approve_resumes_a_blocked_edit(tmp_path):
    (tmp_path / "a.py").write_text("original\n")
    app = make_app(default=[_edit("original", "changed"), FakeProvider.text("done")])
    async with client(app) as c:
        _, first = await turn(c, "t1", tmp_path, "edit it")
        assert first[-1]["event"] == "approval_needed"
        assert (tmp_path / "a.py").read_text() == "original\n"
        call_id = first[-1]["value"]["action_requests"][0]["call_id"]
        ok = await c.post(
            "/sessions/t1/approve", json={"call_id": call_id, "decision": {"type": "approve"}}
        )
        assert ok.json() == {"ok": True}
        # the client left before deciding: it re-attaches to the same run
        async with c.stream(
            "GET", "/sessions/t1/events", params={"after": first[-1]["index"]}
        ) as resp:
            rest = [line async for line in resp.aiter_lines() if line.startswith("data:")]
    assert (tmp_path / "a.py").read_text() == "changed\n"
    assert '"turn_done"' in rest[-1]


async def test_reject_with_a_message_reaches_the_model(tmp_path):
    (tmp_path / "a.py").write_text("original\n")
    app = make_app(default=[_edit("original", "changed"), FakeProvider.text("ok, left it")])
    async with client(app) as c:
        _, events = await turn(
            c, "t1", tmp_path, "edit it", decide={"type": "reject", "message": "not now"}
        )
        provider = (await app.state.runtimes.peek("t1").bundle()).agent.provider
    assert events[-1]["event"] == "turn_done"
    assert (tmp_path / "a.py").read_text() == "original\n"
    results = provider.requests[-1].messages[-1].results
    assert "not now" in results[0].text
