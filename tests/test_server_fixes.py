"""Review fix round 1 (Task 30): follower leaks, /events race, eviction and delete guards."""

import asyncio
import json

from luna_core.providers.fake import FakeProvider

from tests.server_helpers import client, make_app, turn


def _write():
    return [
        FakeProvider.call("write_file", {"file_path": "a.txt", "content": "x"}, "c1"),
        FakeProvider.text("done"),
    ]


async def _asgi(app, method, path, body=None, *, query=b"", on_start=None, gone=False):
    """Call the app directly; ``on_start`` runs inside ``send(http.response.start)``.

    ``gone``: the client disconnects right after sending the request.
    """
    payload = json.dumps(body).encode() if body is not None else b""
    sent = False
    done = asyncio.Event()
    chunks: list[bytes] = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        if gone:
            return {"type": "http.disconnect"}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start" and on_start is not None:
            await on_start()
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                done.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "headers": [(b"authorization", b"Bearer t"), (b"content-type", b"application/json")],
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "server": ("luna", 80),
        "client": ("127.0.0.1", 1),
        "root_path": "",
    }
    await asyncio.wait_for(app(scope, receive, send), 5)
    text = b"".join(chunks).decode()
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


async def _slow_start():
    await asyncio.sleep(0.05)  # the disconnect wins before the stream's first step


async def test_a_message_client_gone_before_the_first_event_leaves_no_listener(tmp_path):
    app = make_app(default=_write())
    async with client(app):
        body = {"workdir": str(tmp_path), "content": "write"}
        await _asgi(app, "POST", "/sessions/t1/messages", body, on_start=_slow_start, gone=True)
        assert not app.state.manager.has_listeners("t1")
        assert app.state.followers == {}
        assert app.state.runtimes.peek("t1")._listeners == []


async def test_an_events_client_gone_before_the_first_event_leaves_no_listener(tmp_path):
    app = make_app(default=_write())
    async with client(app) as c:
        _, events = await turn(c, "t1", tmp_path, "write")
        assert events[-1]["event"] == "approval_needed"
        await _asgi(app, "GET", "/sessions/t1/events", on_start=_slow_start, gone=True)
        assert not app.state.manager.has_listeners("t1")
        assert app.state.followers == {}


async def test_a_failing_start_leaves_no_listener(tmp_path, monkeypatch):
    from luna.server import turns

    async def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(turns, "runtime_for", boom)
    app = make_app()
    async with client(app) as c:
        try:
            await c.post("/sessions/t1/messages", json={"workdir": str(tmp_path), "content": "x"})
        except RuntimeError:
            pass
        assert not app.state.manager.has_listeners("t1") and app.state.followers == {}


async def _paused(c, app, tmp_path):
    _, events = await turn(c, "t1", tmp_path, "write")
    call_id = events[-1]["value"]["action_requests"][0]["call_id"]
    runtime = app.state.runtimes.peek("t1")

    async def finish_now():
        # the turn ends between the handler's snapshot and the stream's first step
        runtime.session.decide(call_id, {"type": "approve"})
        await app.state.manager.wait("t1")

    return events, finish_now


async def test_events_never_invent_a_cancel_when_the_turn_ends_during_the_handshake(tmp_path):
    app = make_app(default=_write())
    async with client(app) as c:
        _, finish_now = await _paused(c, app, tmp_path)
        events = await _asgi(app, "GET", "/sessions/t1/events", on_start=finish_now)
    names = [e["event"] for e in events]
    assert names[-1] == "turn_done" and "cancelled" not in names


async def test_events_after_the_last_index_still_get_the_tail(tmp_path):
    app = make_app(default=_write())
    async with client(app) as c:
        first, finish_now = await _paused(c, app, tmp_path)
        query = f"after={first[-1]['index']}".encode()
        events = await _asgi(app, "GET", "/sessions/t1/events", query=query, on_start=finish_now)
    assert events and events[-1]["event"] == "turn_done"


async def test_out_of_turn_events_during_the_start_reach_the_message_stream(tmp_path, monkeypatch):
    from luna_core.events import Notice

    from luna.turn import engine

    real = engine.before_turn

    def noisy(state):
        real(state)
        app.state.runtimes.peek("t1").session.notify(Notice("info", "from a background task"))

    monkeypatch.setattr(engine, "before_turn", noisy)
    app = make_app()
    async with client(app) as c:
        _, events = await turn(c, "t1", tmp_path, "hi")
    assert {"event": "notice", "level": "info", "text": "from a background task"} in events


async def test_a_finishing_runtime_is_not_evicted(tmp_path):
    from luna_core.background import AgentManager
    from luna_core.session import EventLog

    from luna.server.runtime import RuntimeRegistry

    async def build(runtime):
        return None

    reg = RuntimeRegistry(
        build, log=EventLog(tmp_path / "l.db"), manager=AgentManager(), capacity=1
    )
    first = await reg.get("t1", str(tmp_path))
    reg.finishing.add("t1")
    await reg.get("t2", str(tmp_path))
    assert reg.peek("t1") is first


async def test_on_done_marks_the_runtime_finishing_until_after_turn_ran(tmp_path):
    app = make_app()
    seen = []
    async with client(app) as c:
        runtime = await app.state.runtimes.get("t1", str(tmp_path))
        real = runtime.after_turn

        async def spy():
            seen.append("t1" in app.state.runtimes.finishing)
            await real()

        runtime.after_turn = spy
        await turn(c, "t1", tmp_path, "hi")
        await asyncio.gather(*app.state.finishing)
    assert seen == [True] and app.state.runtimes.finishing == set()


async def test_no_runtime_can_be_created_while_a_session_is_deleted(tmp_path):
    app = make_app()
    async with client(app) as c:
        sid = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        runtime = await app.state.runtimes.get(sid, str(tmp_path))
        gate = asyncio.Event()
        real = runtime.aclose

        async def slow_close():
            await gate.wait()
            await real()

        runtime.aclose = slow_close
        deleting = asyncio.create_task(c.delete(f"/sessions/{sid}"))
        await asyncio.sleep(0.05)
        resp, _ = await turn(c, sid, tmp_path, "hi")
        gate.set()
        assert (await deleting).json() == {"deleted": True}
    assert resp.status_code == 409 and resp.json() == {"error": "session_busy"}
    assert app.state.log.info(sid) is None


async def test_a_session_with_live_background_tasks_is_not_deleted(tmp_path):
    app = make_app()
    async with client(app) as c:
        sid = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        runtime = await app.state.runtimes.get(sid, str(tmp_path))
        never = asyncio.Event()
        runtime.background.start("long", never.wait, session=runtime.session)
        resp = await c.delete(f"/sessions/{sid}")
    assert resp.status_code == 409 and resp.json() == {"error": "session_busy"}
