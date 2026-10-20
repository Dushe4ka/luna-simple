import asyncio

from luna_core.providers.fake import FakeProvider

from tests.server_helpers import client, make_app, strip_index, turn


def _write_call(path="a.txt", call_id="w1"):
    return FakeProvider.call("write_file", {"file_path": path, "content": "x"}, call_id)


def _project(tmp_path, *, verify="", yolo=False):
    (tmp_path / ".luna.toml").write_text(
        "[agent]\n"
        f'verify_command = "{verify}"\nformat_command = ""\ndiagnose_command = ""\n'
        f"yolo = {'true' if yolo else 'false'}\n"
    )


async def test_two_sessions_in_one_workdir_are_isolated(tmp_path):
    app = make_app()
    async with client(app):
        one = await app.state.runtimes.get("t1", str(tmp_path))
        one.set_plan(True)
        two = await app.state.runtimes.get("t2", str(tmp_path))
        assert two.state.plan is False
        assert (await one.bundle()).agent is not (await two.bundle()).agent
        assert (await one.bundle()).agent.policy.planning is True
        assert (await two.bundle()).agent.policy.planning is False


async def test_second_turn_while_the_session_lock_is_held_is_409(tmp_path):
    app = make_app()
    async with client(app) as c:
        runtime = await app.state.runtimes.get("t1", str(tmp_path))
        await runtime.lock.acquire()
        try:
            resp, _ = await turn(c, "t1", tmp_path, "hi")
        finally:
            runtime.lock.release()
    assert resp.status_code == 409 and resp.json() == {"error": "session_busy"}


async def test_abandoned_approval_keeps_the_session_busy_until_cancelled(tmp_path):
    """The client left while a turn waits for approval: it is still that turn."""
    app = make_app(default=[_write_call(), FakeProvider.text("done"), FakeProvider.text("ok")])
    async with client(app) as c:
        _, first = await turn(c, "t1", tmp_path, "write")
        assert first[-1]["event"] == "approval_needed"
        busy, _ = await turn(c, "t1", tmp_path, "hi")
        assert busy.status_code == 409
        assert (await c.post("/sessions/t1/cancel")).json() == {"cancelled": True}
        await app.state.manager.wait("t1")
        resp, events = await turn(c, "t1", tmp_path, "hi")
    assert resp.status_code == 200 and events[-1]["event"] == "turn_done"
    assert not (tmp_path / "a.txt").exists()


async def test_mutating_turn_with_failing_verify_streams_notices_and_fixup(tmp_path, monkeypatch):
    from luna.turn import engine

    results = iter([(False, "boom"), (True, "")])
    monkeypatch.setattr(engine, "run_verify", lambda cmd, wd: next(results))
    _project(tmp_path, verify="make check", yolo=True)
    app = make_app(
        default=[_write_call(), FakeProvider.text("wrote it"), FakeProvider.text("fixed it")]
    )
    async with client(app) as c:
        _, events = await turn(c, "t1", tmp_path, "write a file")
    events = strip_index(events)
    notices = [e for e in events if e["event"] == "notice"]
    assert {"event": "notice", "level": "warn", "text": "verify failed\nboom"} in notices
    assert {"event": "notice", "level": "ok", "text": "✓ verify ok"} in notices
    texts = "".join(e.get("text", "") for e in events if e["event"] == "text_delta")
    assert "fixed it" in texts
    assert [e["event"] for e in events].count("turn_done") == 1


async def test_pinned_files_reach_the_model(tmp_path):
    (tmp_path / "keep.md").write_text("PINNED-CONTENT\n")
    app = make_app()
    async with client(app) as c:
        runtime = await app.state.runtimes.get("t1", str(tmp_path))
        runtime.state.pinned.add("keep.md")
        await turn(c, "t1", tmp_path, "hi")
        provider = (await runtime.bundle()).agent.provider
    assert "PINNED-CONTENT" in provider.requests[0].messages[-1].text


async def test_turn_is_prepared_only_while_holding_the_session_lock(tmp_path, monkeypatch):
    """No TOCTOU: two racing messages must never prepare concurrently."""
    from luna.turn import engine

    app = make_app()
    seen = []
    real = engine.prepare_turn

    def spy(state, *a, **k):
        seen.append(app.state.runtimes.peek("t1").lock.locked())
        return real(state, *a, **k)

    monkeypatch.setattr(engine, "prepare_turn", spy)
    async with client(app) as c:
        await turn(c, "t1", tmp_path, "hi")
    assert seen == [True]


async def test_racing_messages_get_one_turn_and_one_409(tmp_path):
    app = make_app()
    async with client(app) as c:
        results = await asyncio.gather(
            turn(c, "t1", tmp_path, "one"), turn(c, "t1", tmp_path, "two")
        )
    assert sorted(resp.status_code for resp, _ in results) == [200, 409]


async def test_a_failing_prepare_reports_a_readable_error(tmp_path, monkeypatch):
    from luna.turn import engine

    def boom(*a, **k):
        raise ValueError("bad .luna.toml")

    monkeypatch.setattr(engine, "prepare_turn", boom)
    app = make_app()
    async with client(app) as c:
        _, events = await turn(c, "t1", tmp_path, "hi")
        runtime = app.state.runtimes.peek("t1")
    assert events == [
        {
            "event": "error",
            "message": "Не удалось подготовить ход: ValueError: bad .luna.toml",
            "kind": "error",
        }
    ]
    assert not runtime.lock.locked() and not app.state.runtimes.running("t1")


async def test_client_disconnect_mid_turn_lets_the_turn_finish(tmp_path):
    """Closing the TUI mid-stream only unsubscribes; the session ends idle."""
    _project(tmp_path, yolo=True)
    app = make_app(
        default=[
            FakeProvider.call("execute", {"command": "sleep 0.2"}, "c1"),
            FakeProvider.text("a"),
        ]
    )
    async with client(app) as c:
        async with c.stream(
            "POST", "/sessions/t1/messages", json={"workdir": str(tmp_path), "content": "hi"}
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    break  # first event arrives, then the client goes away
        info = await app.state.manager.wait("t1")
        runtime = app.state.runtimes.peek("t1")
    assert info.status == "done"
    assert not runtime.lock.locked() and not runtime.session.busy


async def test_reload_after_flag_on_a_message_rebuilds_after_the_turn(tmp_path):
    built = []
    app = make_app(built=built)
    async with client(app) as c:
        _, events = await turn(c, "t1", tmp_path, "WRITE AGENTS.md", reload_after=True)
        await asyncio.gather(*app.state.finishing)
    assert events[-1]["event"] == "turn_done"
    assert built == ["t1", "t1"]


async def test_reload_requested_by_a_tool_rebuilds_after_the_turn(tmp_path):
    skill = tmp_path / ".luna" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody\n")
    _project(tmp_path, yolo=True)
    built = []
    app = make_app(
        default=[
            FakeProvider.call("manage_skills", {"action": "remove", "name": "demo"}, "m1"),
            FakeProvider.text("removed"),
        ],
        built=built,
    )
    async with client(app) as c:
        _, events = await turn(c, "t1", tmp_path, "drop the demo skill")
        await asyncio.gather(*app.state.finishing)
    assert any(e["event"] == "reload_requested" for e in events)
    assert not skill.exists() and built == ["t1", "t1"]
