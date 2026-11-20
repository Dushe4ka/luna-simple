"""Team board and message tools."""

import pytest
from luna_core.team.board import LEAD, TeamStore
from luna_core.team.messages import Mailbox
from luna_core.team.tools import TeamContext, lead_tools, member_tools
from luna_core.tools.base import ToolContext


@pytest.fixture
def board(tmp_path):
    store = TeamStore(tmp_path / "s.db")
    rid = store.create_run(workdir="/w", task="t", session_id="s", settings={})
    store.add_member(rid, LEAD, {"role": "lead"})
    store.add_member(rid, "dev", {})
    store.add_member(rid, "ops", {})
    store.add_task(rid, title="first")
    store.add_task(rid, title="second", deps=["T1"])
    yield store, rid
    store.close()


def by_name(tools):
    return {t.name: t for t in tools}


async def call(tool, **args):
    out = await tool.invoke(ToolContext(), args)
    return out.is_error, out.content[0].text


async def test_member_tools(board):
    store, rid = board
    ctx = TeamContext(store, Mailbox(store), rid, "dev", task_id="T1")
    tools = by_name(member_tools(ctx))
    assert set(tools) == {"board_list", "board_update", "send_message", "read_messages"}
    _err, text = await call(tools["board_list"])
    assert "T1 [pending] first (code)" in text and "T2 [pending] second (code) deps T1" in text
    assert "members: dev (idle), ops (idle)" in text
    err, _text = await call(tools["board_update"], status="done", note="did it")
    assert not err and ctx.reports == {"T1": ("done", "did it")}
    err, _text = await call(tools["board_update"], status="weird", note="")
    assert err
    err, _text = await call(tools["send_message"], to="ops", text="hi")
    assert not err
    err, text = await call(tools["send_message"], to="*", text="all")
    assert err and "only the lead" in text
    ops = by_name(member_tools(TeamContext(store, Mailbox(store), rid, "ops")))
    assert (await call(ops["read_messages"]))[1] == "from dev: hi"
    assert (await call(ops["read_messages"]))[1] == "no new messages"
    err, text = await call(ops["board_update"], status="done", note="x")
    assert err and "no task" in text


async def test_every_result_carries_the_stop_note_while_stopping(board):
    store, rid = board
    ctx = TeamContext(store, Mailbox(store), rid, "dev", task_id="T1", stopping=lambda: True)
    _err, text = await call(by_name(member_tools(ctx))["board_list"])
    assert text.startswith("STOP:")


async def test_lead_tools_change_the_board_and_log_decisions(board):
    store, rid = board
    changes = []
    ctx = TeamContext(
        store,
        Mailbox(store),
        rid,
        LEAD,
        on_change=lambda: changes.append(1),
        hire=lambda args: f"hired {args['name']}",
    )
    tools = by_name(lead_tools(ctx))
    assert "board_update" not in tools
    assert {"board_add", "board_split", "hire", "retire", "reassign", "broadcast"} <= set(tools)
    _err, text = await call(
        tools["board_add"], title="third", kind="research", deps=["T1"], member="ops"
    )
    assert text == "added T3" and store.task(rid, "T3").assignee == "ops"
    _err, text = await call(
        tools["board_split"], task_id="T2", parts=[{"title": "a"}, {"title": "b"}]
    )
    assert text == "T2 split into T4, T5"
    assert store.task(rid, "T2") is None and store.task(rid, "T4").deps == ("T1",)
    await call(tools["reassign"], task_id="T4", member="dev")
    assert store.task(rid, "T4").assignee == "dev"
    await call(tools["retire"], member="dev")
    assert store.member(rid, "dev").status == "retired" and store.task(rid, "T4").assignee is None
    err, _text = await call(tools["reassign"], task_id="T5", member="dev")
    assert err
    err, _text = await call(tools["board_split"], task_id="T4", parts=[{"title": "only"}])
    assert err
    _err, text = await call(tools["broadcast"], text="wrap up")
    assert text == "sent to 1 members"
    _err, text = await call(tools["hire"], name="qa", prompt="test things")
    assert text == "hired qa"
    decisions = [e.data["text"] for e in store.events(rid, kinds=["decision"])]
    assert decisions == ["added T3: third", "split T2 into T4, T5", "T4 → dev", "retired dev"]
    assert changes


async def test_a_working_member_cannot_be_retired(board):
    store, rid = board
    store.claim_next(rid, "dev")
    store.update_member(rid, "dev", status="working")
    ctx = TeamContext(store, Mailbox(store), rid, LEAD)
    err, text = await call(by_name(lead_tools(ctx))["retire"], member="dev")
    assert err and "working" in text
