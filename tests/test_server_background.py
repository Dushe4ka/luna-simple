import asyncio
import json

from luna_core.events import ApprovalRequest, BackgroundDone, NeedsApproval
from luna_core.providers.fake import FakeProvider

from tests.server_helpers import client, make_app, sse


async def test_turn_streams_and_approval_is_a_json_call(tmp_path):
    app = make_app(
        default=[
            FakeProvider.call("write_file", {"file_path": "a.txt", "content": "x"}, "c1"),
            FakeProvider.text("done"),
        ]
    )
    async with client(app) as c:
        sid = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        events = []
        async with c.stream(
            "POST", f"/sessions/{sid}/messages", json={"workdir": str(tmp_path), "content": "write"}
        ) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:])
                events.append(event)
                if event["event"] == "approval_needed":
                    call_id = event["value"]["action_requests"][0]["call_id"]
                    ok = await c.post(
                        f"/sessions/{sid}/approve",
                        json={"call_id": call_id, "decision": {"type": "approve"}},
                    )
                    assert ok.json() == {"ok": True}
        assert events[-1]["event"] == "turn_done" and (tmp_path / "a.txt").read_text() == "x"
        history = (
            await c.get(f"/sessions/{sid}/messages", params={"workdir": str(tmp_path)})
        ).json()["messages"]
        assert [h["role"] for h in history] == ["user", "tool", "assistant"]
        assert history[1]["name"] == "write_file"


async def test_background_session_survives_client_disconnect(tmp_path):
    app = make_app(
        default=[
            FakeProvider.call("execute", {"command": "sleep 0.3"}, "c1"),
            FakeProvider.text("finished"),
        ]
    )
    async with client(app) as c:
        sid = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        runtime = await app.state.runtimes.get(sid, str(tmp_path))
        (await runtime.bundle()).agent.policy.yolo = True
        async with c.stream(
            "POST", f"/sessions/{sid}/messages", json={"workdir": str(tmp_path), "content": "go"}
        ) as resp:
            async for line in resp.aiter_lines():
                if "tool_started" in line:
                    break  # the TUI closes mid-turn
        assert not app.state.manager.has_listeners(sid)  # disconnect only unsubscribed
        info = await app.state.manager.wait(sid)
        assert info.status == "done"
        await asyncio.gather(*app.state.finishing)
        history = (
            await c.get(f"/sessions/{sid}/messages", params={"workdir": str(tmp_path)})
        ).json()["messages"]
        # the finished transcript, then the completion notice nobody saw live
        assert history[-2] == {"role": "assistant", "content": "finished"}
        assert history[-1] == {
            "role": "notice",
            "level": "ok",
            "text": "фоновая сессия «go» завершена",
        }
        runs = (await c.get("/agents")).json()["runs"]
        assert runs[0]["session_id"] == sid and runs[0]["status"] == "done"


async def test_a_watched_run_logs_no_completion_notice(tmp_path):
    app = make_app()
    async with client(app) as c:
        sid = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        async with c.stream(
            "POST", f"/sessions/{sid}/messages", json={"workdir": str(tmp_path), "content": "hi"}
        ) as resp:
            events = await sse(resp)
        await asyncio.gather(*app.state.finishing)
        history = (
            await c.get(f"/sessions/{sid}/messages", params={"workdir": str(tmp_path)})
        ).json()["messages"]
    assert events[-1]["event"] == "turn_done"
    assert [h["role"] for h in history] == ["user", "assistant"]


async def test_background_endpoint_attach_and_cancel(tmp_path):
    app = make_app(default=[FakeProvider.call("execute", {"command": "sleep 30"}, "c1")])
    async with client(app) as c:
        sid = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        runtime = await app.state.runtimes.get(sid, str(tmp_path))
        (await runtime.bundle()).agent.policy.yolo = True
        info = (
            await c.post(
                f"/sessions/{sid}/background", json={"workdir": str(tmp_path), "content": "long"}
            )
        ).json()
        assert info["status"] in ("queued", "running")
        busy = await c.post(
            f"/sessions/{sid}/messages", json={"workdir": str(tmp_path), "content": "again"}
        )
        assert busy.status_code == 409
        while not any(e["event"] == "tool_started" for e in app.state.manager.events(sid)):
            await asyncio.sleep(0.02)
        listed = (await c.get("/sessions", params={"workdir": str(tmp_path)})).json()["sessions"]
        assert listed[0]["running"] is True and listed[0]["background"] is True
        assert (await c.post(f"/sessions/{sid}/cancel")).json() == {"cancelled": True}
        async with c.stream("GET", f"/sessions/{sid}/events", params={"after": -1}) as resp:
            events = await sse(resp)
        assert events[0]["event"] == "turn_started" and events[-1]["event"] == "cancelled"
        listed = (await c.get("/sessions", params={"workdir": str(tmp_path)})).json()["sessions"]
        assert listed[0]["id"] == sid and listed[0]["running"] is False


async def test_events_resume_after_an_index(tmp_path):
    app = make_app(default=[FakeProvider.text("one"), FakeProvider.text("two")])
    async with client(app) as c:
        sid = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        async with c.stream(
            "POST", f"/sessions/{sid}/messages", json={"workdir": str(tmp_path), "content": "hi"}
        ) as resp:
            first = await sse(resp)
        async with c.stream("GET", f"/sessions/{sid}/events", params={"after": 1}) as resp:
            replay = await sse(resp)
    assert [e["index"] for e in first] == list(range(len(first)))
    assert replay == first[2:]


async def test_delete_session(tmp_path):
    app = make_app()
    async with client(app) as c:
        sid = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        assert (await c.delete(f"/sessions/{sid}")).json() == {"deleted": True}
        assert (await c.get("/sessions", params={"workdir": str(tmp_path)})).json()[
            "sessions"
        ] == []


async def test_sessions_skip_children_and_empty_sessions(tmp_path):
    app = make_app()
    async with client(app) as c:
        empty = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        sid = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        async with c.stream(
            "POST", f"/sessions/{sid}/messages", json={"workdir": str(tmp_path), "content": "hi"}
        ) as resp:
            await sse(resp)
        child = app.state.log.create(str(tmp_path), parent_id=sid, meta={"kind": "subagent"})
        app.state.log.append(child, "user", {"message": {"role": "user", "content": []}})
        rows = (await c.get("/sessions", params={"workdir": str(tmp_path)})).json()["sessions"]
    assert [r["id"] for r in rows] == [sid]
    assert empty != sid
    assert set(rows[0]) == {
        "id",
        "title",
        "created",
        "updated",
        "group",
        "relative_time",
        "running",
        "background",
    }


async def test_out_of_turn_events_reach_an_open_stream_and_a_late_attach(tmp_path):
    """session.notify events (background subagents) are not in the run buffer."""
    app = make_app()
    async with client(app) as c:
        sid = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        runtime = await app.state.runtimes.get(sid, str(tmp_path))
        child = app.state.log.create(str(tmp_path), parent_id=sid)
        from luna_core.session import Session

        child_session = Session(app.state.log, child, workdir=str(tmp_path), undo=False)
        runtime.session.adopt(child_session)
        decided = child_session.expect_decision("c9")
        request = ApprovalRequest(f"{child}:c9", "execute", {"command": "ls"}, "ls", "execute:ls *")
        runtime.session.notify(NeedsApproval(request))
        async with c.stream("GET", f"/sessions/{sid}/events") as resp:
            late = await sse(resp)
        assert [e["event"] for e in late] == ["approval_needed"]
        assert late[0]["value"]["action_requests"][0]["call_id"] == f"{child}:c9"
        ok = await c.post(
            f"/sessions/{sid}/approve",
            json={"call_id": f"{child}:c9", "decision": {"type": "approve"}},
        )
        assert ok.json() == {"ok": True} and (await decided).type == "approve"
        assert runtime.pending_out_of_turn() == []

        seen: list[dict] = []
        unlisten = runtime.listen(seen.append)
        runtime.session.notify(BackgroundDone("bg-1", True, "all good"))
        unlisten()
    assert seen == [
        {"event": "background_done", "handle": "bg-1", "ok": True, "summary": "all good"}
    ]


async def test_approve_unknown_call_is_404(tmp_path):
    app = make_app()
    async with client(app) as c:
        sid = (await c.post("/sessions", json={"workdir": str(tmp_path)})).json()["id"]
        missing = await c.post(
            f"/sessions/{sid}/approve", json={"call_id": "x", "decision": {"type": "approve"}}
        )
        await app.state.runtimes.get(sid, str(tmp_path))
        unknown = await c.post(
            f"/sessions/{sid}/approve", json={"call_id": "x", "decision": {"type": "approve"}}
        )
        bad = await c.post(f"/sessions/{sid}/approve", json={"call_id": "x", "decision": {}})
    assert missing.status_code == 404 and unknown.status_code == 404
    assert bad.status_code == 400


async def test_health_reports_the_protocol(tmp_path):
    from luna_core.protocol import PROTOCOL_VERSION

    import luna

    async with client(make_app()) as c:
        body = (await c.get("/health")).json()
    assert body == {"ok": True, "version": luna.__version__, "protocol": PROTOCOL_VERSION}


async def test_the_reference_client_speaks_to_the_real_server(tmp_path):
    """luna_core.client.LunaClient (protocol v1) against this server, end to end."""
    import httpx
    from luna_core.client import LunaClient
    from luna_core.protocol import validate_event

    from luna.server.app import shutdown
    from tests.server_helpers import StreamingASGITransport

    app = make_app(
        default=[
            FakeProvider.call("write_file", {"file_path": "a.txt", "content": "x"}, "c1"),
            FakeProvider.text("done"),
        ]
    )
    http = httpx.AsyncClient(transport=StreamingASGITransport(app), base_url="http://luna")
    luna = LunaClient("http://luna", "t", client=http)
    try:
        assert (await luna.health())["protocol"] == 1
        sid = await luna.create_session(str(tmp_path))
        events = []
        async for event in luna.send(sid, "write", str(tmp_path)):
            events.append(event)
            validate_event({k: v for k, v in event.items() if k != "index"})
            if event["event"] == "approval_needed":
                call_id = event["value"]["action_requests"][0]["call_id"]
                await luna.decide(sid, call_id, {"type": "approve"})
        assert events[-1]["event"] == "turn_done"
        assert [s["id"] for s in await luna.list_sessions(str(tmp_path))] == [sid]
        assert [h["role"] for h in await luna.history(sid, str(tmp_path))] == [
            "user",
            "tool",
            "assistant",
        ]
        state = await luna.state(sid, str(tmp_path))
        assert state["undo_depth"] == 1 and state["running"] is False
        assert (await luna.run_command(sid, "/plan on", str(tmp_path)))["effects"] == {"plan": True}
        assert any(c["name"] == "/plan" for c in await luna.commands(str(tmp_path)))
        assert (await luna.agents())["runs"][0]["session_id"] == sid
        replay = [e async for e in luna.attach(sid)]
        assert replay == events
        assert await luna.cancel(sid) is False
        await luna.delete_session(sid)
        assert await luna.list_sessions(str(tmp_path)) == []
    finally:
        await luna.aclose()
        await shutdown(app)
