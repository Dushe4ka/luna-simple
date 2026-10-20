import threading

from luna_core.messages import Message, Text, ToolCall, ToolResult, message_to_dict
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session

from tests.server_helpers import client, make_app, strip_index, turn


def _session(tmp_path) -> Session:
    return Session.create(EventLog(tmp_path / "log.db"), str(tmp_path), undo=False)


def _add(session: Session, message: Message) -> None:
    kind = "tool_results" if message.role == "tool" else message.role
    session.append(kind, {"message": message_to_dict(message)})


async def test_get_history_includes_tool_calls_in_order(tmp_path):
    (tmp_path / "README.md").write_text("# Luna\n")
    app = make_app(
        default=[
            FakeProvider.call("read_file", {"file_path": "README.md"}, "c1"),
            FakeProvider.text("it parses the config file"),
        ]
    )
    async with client(app) as c:
        await turn(c, "t1", tmp_path, "what does this repo do")
        resp = await c.get("/sessions/t1/messages", params={"workdir": str(tmp_path)})
    messages = resp.json()["messages"]
    assert messages[0] == {"role": "user", "content": "what does this repo do"}
    assert {k: messages[1][k] for k in ("role", "name", "args_preview", "ok")} == {
        "role": "tool",
        "name": "read_file",
        "args_preview": "README.md",
        "ok": True,
    }
    assert "# Luna" in messages[1]["detail"]
    assert messages[2] == {"role": "assistant", "content": "it parses the config file"}


def test_history_shows_only_text_from_block_content(tmp_path):
    from luna.server.turns import history_entries

    session = _session(tmp_path)
    _add(session, Message.assistant([Text("Сейчас посмотрю."), ToolCall("c1", "ls", {})]))
    assert history_entries(session.records()) == [
        {"role": "assistant", "content": "Сейчас посмотрю."}
    ]


def test_history_marks_errored_tool_calls(tmp_path):
    from luna.server.turns import history_entries

    session = _session(tmp_path)
    _add(session, Message.assistant([ToolCall("c1", "execute", {"command": "false"})]))
    _add(
        session,
        Message.tool_results([ToolResult("c1", (Text("exit 1"),), is_error=True, name="execute")]),
    )
    assert history_entries(session.records()) == [
        {
            "role": "tool",
            "name": "execute",
            "args_preview": "false",
            "ok": False,
            "detail": "exit 1",
        }
    ]


def test_history_hides_undone_turns(tmp_path):
    from luna.server.turns import history_entries

    session = _session(tmp_path)
    for turn_id, text in (("a", "kept"), ("b", "undone")):
        session.append("turn_started", {"turn_id": turn_id})
        _add(session, Message.user(text))
    session.append("rewound", {"turn_id": "b"})
    assert history_entries(session.records()) == [{"role": "user", "content": "kept"}]


async def test_tool_started_event_carries_args_preview(tmp_path):
    app = make_app(
        default=[FakeProvider.call("ls", {"path": "погода Орёл"}, "c1"), FakeProvider.text("ok")]
    )
    async with client(app) as c:
        _, events = await turn(c, "t1", tmp_path, "look")
    started = next(e for e in events if e["event"] == "tool_started")
    assert started["args_preview"] == "погода Орёл"


async def test_get_history_on_a_fresh_session_is_empty(tmp_path):
    async with client(make_app()) as c:
        resp = await c.get("/sessions/never-used/messages", params={"workdir": str(tmp_path)})
    assert resp.status_code == 200
    assert resp.json() == {"messages": []}


async def test_a_turn_that_raises_surfaces_an_error_event_not_silence(tmp_path):
    """Regression: a provider failure (e.g. HTTP 402) must end the stream with ``error``."""
    app = make_app(default=[[RuntimeError("insufficient balance")]])
    async with client(app) as c:
        _, events = await turn(c, "boom", tmp_path, "hi")
    assert events[-1]["event"] == "error" and "insufficient balance" in events[-1]["message"]


async def test_message_streams_text_delta_and_turn_done(tmp_path):
    app = make_app(default=[FakeProvider.text("hello from the agent")])
    async with client(app) as c:
        resp, events = await turn(c, "abc", tmp_path, "hi")
    assert resp.status_code == 200
    assert {"event": "text_delta", "text": "hello from the agent"} in strip_index(events)
    assert events[-1]["event"] == "turn_done"


async def test_message_records_session_title_from_first_message(tmp_path):
    app = make_app()
    async with client(app) as c:
        await turn(c, "abc", tmp_path, "please help me fix this bug")
    rows = app.state.log.list(str(tmp_path))
    assert [(r.id, r.title) for r in rows] == [("abc", "please help me fix this bug")]


def _edit_a_py(tmp_path):
    """A scripted turn that edits a.py, then answers."""
    (tmp_path / "a.py").write_text("original\n")
    args = {"file_path": "a.py", "old_string": "original", "new_string": "changed"}
    return [FakeProvider.call("edit_file", args, "e1"), FakeProvider.text("done")]


async def test_message_that_pauses_on_approval_still_bumps_updated(tmp_path):
    """A session waiting for an approval sorts as recently active."""
    import time

    app = make_app(default=[FakeProvider.text("ok"), *_edit_a_py(tmp_path)])
    async with client(app) as c:
        await turn(c, "t3", tmp_path, "first message")
        first_updated = app.state.log.info("t3").updated
        time.sleep(0.01)
        before_second_send = time.time()
        _, events = await turn(c, "t3", tmp_path, "edit it")
        assert events[-1]["event"] == "approval_needed"
        second_updated = app.state.log.info("t3").updated
    assert second_updated >= before_second_send > first_updated


async def _send_edit(tmp_path, session_id: str) -> list[dict]:
    app = make_app(default=_edit_a_py(tmp_path))
    async with client(app) as c:
        _, events = await turn(c, session_id, tmp_path, "edit it")
    return events


async def test_an_allow_rule_auto_approves_without_prompting(tmp_path):
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text('allow = ["edit_file:a.py"]\n')
    events = await _send_edit(tmp_path, "allowed")
    assert not any(e["event"] == "approval_needed" for e in events)
    assert events[-1]["event"] == "turn_done"
    assert (tmp_path / "a.py").read_text() == "changed\n"


async def test_a_deny_rule_auto_rejects_without_prompting(tmp_path):
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text('deny = ["edit_file:a.py"]\n')
    events = await _send_edit(tmp_path, "denied")
    assert not any(e["event"] == "approval_needed" for e in events)
    assert events[-1]["event"] == "turn_done"
    assert (tmp_path / "a.py").read_text() == "original\n"


async def test_no_matching_rule_still_asks_the_client(tmp_path):
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text(
        'allow = ["edit_file:somewhere-else.py"]\n'
    )
    events = await _send_edit(tmp_path, "asked")
    assert events[-1]["event"] == "approval_needed"
    assert not any(e["event"] == "turn_done" for e in events)
    assert (tmp_path / "a.py").read_text() == "original\n"


async def test_history_is_read_off_the_event_loop(tmp_path, monkeypatch):
    seen = []
    real = Session.records

    def spy(self):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(self)

    async with client(make_app()) as c:
        monkeypatch.setattr(Session, "records", spy)
        await c.get("/sessions/t1/messages", params={"workdir": str(tmp_path)})
    assert seen == [False]


async def test_images_in_the_body_reach_the_model(tmp_path):
    built = []
    app = make_app(built=built)
    async with client(app) as c:
        await turn(c, "img", tmp_path, "look", images=[{"media_type": "image/png", "data": "aGk="}])
        bundle = await (await app.state.runtimes.get("img", str(tmp_path))).bundle()
    sent = bundle.agent.provider.requests[0].messages[-1]
    assert any(getattr(block, "media_type", None) == "image/png" for block in sent.content)
