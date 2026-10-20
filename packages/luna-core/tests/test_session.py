import json
import subprocess

import pytest
from luna_core.errors import LunaCoreError
from luna_core.messages import Message, Text, ToolCall, ToolResult, message_to_dict
from luna_core.policy import Decision
from luna_core.session import EventLog, Session, patch_dangling
from luna_core.usage import Usage


@pytest.fixture
def log(tmp_path):
    lg = EventLog(tmp_path / "sessions.db")
    yield lg
    lg.close()


def turn(session, turn_id, user, reply, usage=None):
    usage = usage if usage is not None else Usage(10, 2)
    session.append("turn_started", {"turn_id": turn_id})
    session.append("user", {"message": message_to_dict(Message.user(user))})
    session.append(
        "assistant",
        {"message": message_to_dict(Message.assistant([Text(reply)])), "usage": usage.to_dict()},
    )
    session.append(
        "turn_done",
        {"turn_id": turn_id, "usage": usage.to_dict(), "cost": 0.01, "stop_reason": "end"},
    )


def test_log_create_append_list(log, tmp_path):
    sid = log.create(str(tmp_path), title="hello")
    assert log.append(sid, "user", {"x": 1}) == 1 and log.append(sid, "user", {"x": 2}) == 2
    assert [r.payload["x"] for r in log.records(sid)] == [1, 2]
    assert [r.seq for r in log.records(sid, after=1)] == [2]
    assert log.list(str(tmp_path))[0].title == "hello"
    assert log.delete(sid) and log.records(sid) == [] and log.info(sid) is None


def test_session_messages_usage_cost_title(log, tmp_path):
    s = Session.create(log, str(tmp_path), undo=False)
    turn(s, "t1", "first question", "answer one")
    turn(s, "t2", "second", "answer two")
    assert [m.text for m in s.messages()] == [
        "first question",
        "answer one",
        "second",
        "answer two",
    ]
    assert s.usage() == Usage(20, 4) and s.cost() == pytest.approx(0.02)
    assert s.title == "first question" and s.turn_ids() == ["t1", "t2"]
    reopened = Session.open(log, s.id, undo=False)
    assert [m.text for m in reopened.messages()] == [m.text for m in s.messages()]


def test_compaction_replaces_old_messages_with_a_summary(log, tmp_path):
    s = Session.create(log, str(tmp_path), undo=False)
    turn(s, "t1", "a", "b")
    upto = s.records()[-1].seq
    turn(s, "t2", "c", "d")
    s.append("compacted", {"summary": "talked about a", "upto_seq": upto, "removed": 2})
    msgs = s.messages()
    assert "talked about a" in msgs[0].text and msgs[0].role == "user"
    assert [m.text for m in msgs[1:]] == ["c", "d"]
    assert len(s.records()) > 8  # the full history stays in the log


def test_patch_dangling_adds_cancelled_results():
    history = [
        Message.user("go"),
        Message.assistant([ToolCall("c1", "ls", {}), ToolCall("c2", "grep", {"pattern": "x"})]),
        Message.tool_results([ToolResult("c1", (Text("a"),), name="ls")]),
        Message.assistant([ToolCall("c3", "ls", {})]),
    ]
    patched = patch_dangling(history)
    assert [r.call_id for r in patched[2].results] == ["c1", "c2"] and patched[2].results[
        1
    ].is_error
    assert patched[4].role == "tool" and patched[4].results[0].call_id == "c3"


def test_undo_redo_rewinds_conversation_without_git(log, tmp_path):
    s = Session.create(log, str(tmp_path), undo=False)
    turn(s, "t1", "a", "b")
    turn(s, "t2", "c", "d")
    assert s.undo_depth == 2
    res = s.undo()
    assert (
        res.turn_id == "t2" and [m.text for m in s.messages()] == ["a", "b"] and s.undo_depth == 1
    )
    assert s.redo().turn_id == "t2" and [m.text for m in s.messages()][-1] == "d"
    s.undo()
    turn(s, "t3", "e", "f")
    assert s.redo() is None  # a new turn clears redo
    assert [m.text for m in s.messages()] == ["a", "b", "e", "f"]


def test_undo_also_reverts_files(log, tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("one")
    s = Session.create(log, str(tmp_path))
    s.append("turn_started", {"turn_id": "t1"})
    s.undo_journal.begin_turn("t1")
    s.append("user", {"message": message_to_dict(Message.user("edit"))})
    s.undo_journal.before_write("t1", "f.txt")  # what the loop does before a file tool
    (tmp_path / "f.txt").write_text("two")
    res = s.undo()
    assert res.turn_id == "t1" and (tmp_path / "f.txt").read_text() == "one" and s.messages() == []


def _init_git(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


# -- review finding (task 17): undo/redo must not assume the journal's newest
# -- record is the conversation's last visible turn (they can diverge when a
# -- turn is cancelled after begin_turn but before any message is logged) --


def test_undo_skips_a_ghost_journal_record_between_two_real_turns(log, tmp_path):
    _init_git(tmp_path)
    (tmp_path / "f.txt").write_text("zero")
    s = Session.create(log, str(tmp_path))

    s.append("turn_started", {"turn_id": "t1"})
    s.undo_journal.begin_turn("t1")
    s.append("user", {"message": message_to_dict(Message.user("a"))})
    s.append(
        "assistant",
        {
            "message": message_to_dict(Message.assistant([Text("b")])),
            "usage": Usage(1, 1).to_dict(),
        },
    )
    s.undo_journal.before_write("t1", "f.txt")
    (tmp_path / "f.txt").write_text("one")

    # a turn cancelled before any message was logged: a journal record with
    # no visible turn in the conversation.
    s.append("turn_started", {"turn_id": "ghost"})
    s.undo_journal.begin_turn("ghost")
    s.undo_journal.before_write("ghost", "f.txt")
    (tmp_path / "f.txt").write_text("ghost")

    s.append("turn_started", {"turn_id": "t2"})
    s.undo_journal.begin_turn("t2")
    s.append("user", {"message": message_to_dict(Message.user("c"))})
    s.append(
        "assistant",
        {
            "message": message_to_dict(Message.assistant([Text("d")])),
            "usage": Usage(1, 1).to_dict(),
        },
    )
    s.undo_journal.before_write("t2", "f.txt")
    (tmp_path / "f.txt").write_text("two")

    res2 = s.undo()
    assert res2.turn_id == "t2"
    assert (tmp_path / "f.txt").read_text() == "ghost"
    assert [m.text for m in s.messages()] == ["a", "b"]

    res1 = s.undo()
    assert res1.turn_id == "t1"
    assert (tmp_path / "f.txt").read_text() == "zero"
    assert s.messages() == []


def test_redo_reports_the_journals_note(log, tmp_path):
    _init_git(tmp_path)
    (tmp_path / "f.txt").write_text("one")
    s = Session.create(log, str(tmp_path))
    s.append("turn_started", {"turn_id": "t1"})
    s.undo_journal.begin_turn("t1")
    s.append("user", {"message": message_to_dict(Message.user("a"))})
    s.append(
        "assistant",
        {
            "message": message_to_dict(Message.assistant([Text("b")])),
            "usage": Usage(1, 1).to_dict(),
        },
    )
    s.undo_journal.before_write("t1", "f.txt")
    (tmp_path / "f.txt").write_text("two")
    s.undo()
    res = s.redo()
    assert res.turn_id == "t1" and "files re-applied" in res.note


def test_undo_without_a_journal_record_leaves_files_untouched(log, tmp_path):
    _init_git(tmp_path)
    (tmp_path / "f.txt").write_text("one")
    s = Session.create(log, str(tmp_path))
    s.append("turn_started", {"turn_id": "t1"})
    # no undo_journal.begin_turn("t1") -- the turn has no journal record at all.
    s.append("user", {"message": message_to_dict(Message.user("a"))})
    s.append(
        "assistant",
        {
            "message": message_to_dict(Message.assistant([Text("b")])),
            "usage": Usage(1, 1).to_dict(),
        },
    )
    (tmp_path / "f.txt").write_text("two")
    res = s.undo()
    assert res.turn_id == "t1"
    assert (tmp_path / "f.txt").read_text() == "two"
    assert "not journaled" in res.note


def test_rewind_to_seq(log, tmp_path):
    s = Session.create(log, str(tmp_path), undo=False)
    turn(s, "t1", "a", "b")
    mark = s.records()[-1].seq
    turn(s, "t2", "c", "d")
    turn(s, "t3", "e", "f")
    assert s.rewind(mark) == 2 and [m.text for m in s.messages()] == ["a", "b"]


def test_fork_copies_history_up_to_seq(log, tmp_path):
    s = Session.create(log, str(tmp_path), undo=False)
    turn(s, "t1", "a", "b")
    mark = s.records()[-1].seq
    turn(s, "t2", "c", "d")
    child = s.fork(mark)
    assert [m.text for m in child.messages()] == ["a", "b"]
    info = log.info(child.id)
    assert info.parent_id == s.id and info.fork_seq == mark


async def test_approvals_and_queue(log, tmp_path):
    s = Session.create(log, str(tmp_path), undo=False)
    fut = s.expect_decision("c1")
    assert s.pending_approvals == ["c1"]
    s.decide("c1", {"type": "reject", "message": "no"})
    assert (await fut) == Decision.reject("no") and s.pending_approvals == []
    with pytest.raises(LunaCoreError):
        s.decide("missing", Decision.approve())
    s.queue("also check tests")
    assert s.drain_queue() == ["also check tests"] and s.drain_queue() == []


async def test_child_approvals_route_through_the_parent_and_cost_adds_up(log, tmp_path):
    parent = Session.create(log, str(tmp_path), undo=False)
    child = Session(
        log,
        log.create(str(tmp_path), parent_id=parent.id, meta={"kind": "subagent"}),
        workdir=str(tmp_path),
        undo=False,
    )
    parent.adopt(child)
    fut = child.expect_decision("k1")
    assert parent.pending_approvals == [f"{child.id}:k1"]
    parent.decide(f"{child.id}:k1", Decision.approve())
    assert (await fut) == Decision.approve()
    turn(child, "c1", "sub", "done")
    turn(parent, "p1", "main", "ok")
    assert parent.cost() == pytest.approx(0.02)
    seen = []
    unsubscribe = parent.subscribe(seen.append)
    parent.notify("ping")
    unsubscribe()
    parent.notify("pong")
    assert seen == ["ping"]


async def test_turn_flags(log, tmp_path):
    s = Session.create(log, str(tmp_path), undo=False)
    assert not s.cancel_turn()
    cancel = s._begin_turn()
    assert s.busy and s.cancel_turn() and cancel.is_set()
    s._end_turn()
    assert not s.busy


def test_export_formats(log, tmp_path):
    s = Session.create(log, str(tmp_path), undo=False)
    turn(s, "t1", "hello <world>", "hi")
    md = s.export("md")
    assert "## You" in md and "hello <world>" in md and "## Luna" in md
    assert "&lt;world&gt;" in s.export("html")
    assert json.loads(s.export("json"))["records"][0]["type"] == "turn_started"


def test_prune(log, tmp_path):
    old = log.create(str(tmp_path))
    log._conn.execute("UPDATE sessions SET updated = 0 WHERE id = ?", (old,))
    log.create(str(tmp_path))
    assert log.prune(older_than_days=30) == 1 and log.info(old) is None


async def test_prefixed_approval_ids_reach_only_the_child(log, tmp_path):
    parent = Session.create(log, str(tmp_path), undo=False)
    child = Session(
        log, log.create(str(tmp_path), parent_id=parent.id), workdir=str(tmp_path), undo=False
    )
    parent.adopt(child)
    mine = parent.expect_decision("call_0")
    theirs = child.expect_decision("call_0")
    parent.decide(f"{child.id}:call_0", Decision.approve())
    assert (await theirs) == Decision.approve()
    assert not mine.done() and parent.pending_approvals == ["call_0"]


# -- final review I2: child approvals are listed with their "<child>:" prefix --


async def test_pending_approvals_prefix_child_ids_and_siblings_do_not_collide(log, tmp_path):
    parent = Session.create(log, str(tmp_path), undo=False)
    a = Session.create(log, str(tmp_path), undo=False)
    b = Session.create(log, str(tmp_path), undo=False)
    grand = Session.create(log, str(tmp_path), undo=False)
    parent.adopt(a)
    parent.adopt(b)
    a.adopt(grand)
    fa, fb, fg = a.expect_decision("c1"), b.expect_decision("c1"), grand.expect_decision("c1")
    assert sorted(parent.pending_approvals) == sorted(
        [f"{a.id}:c1", f"{b.id}:c1", f"{a.id}:{grand.id}:c1"]
    )
    with pytest.raises(LunaCoreError):
        parent.decide("c1", Decision.approve())  # no unprefixed fallback
    parent.decide(f"{b.id}:c1", Decision.approve())
    assert fb.done() and not fa.done()
    parent.decide(f"{a.id}:{grand.id}:c1", Decision.reject("no"))
    assert fg.result().type == "reject" and not fa.done()
