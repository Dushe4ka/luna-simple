"""Approvals over HTTP: a tiny Starlette app that streams a turn and takes decisions.

POST /run      {"text": "..."}                        -> SSE stream of wire events
POST /approve  {"call_id": "...", "decision": {...}}  -> answers a pending approval

Demo only: there is no authentication or origin check, so add both before exposing it.

Needs ``starlette`` (``pip install starlette uvicorn``).

Serve:      python examples/approvals_web.py --workdir . --port 8000
Self-test:  python examples/approvals_web.py --self-test --workdir .
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from luna_core import Agent, EventLog, LunaCoreError, Policy, Session, Workspace, providers, to_wire
from luna_core.providers.fake import FakeProvider
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route


def make_app(agent: Agent, session: Session) -> Starlette:
    """Build the app around one agent and one session."""

    async def run(request: Request) -> StreamingResponse:
        body = await request.json()

        async def sse():
            # aclosing: if the client goes away the turn is closed (cancelled), not leaked.
            events = agent.run(session, body["text"])
            try:
                async for event in events:
                    yield f"data: {json.dumps(to_wire(event))}\n\n"
            finally:
                await events.aclose()

        return StreamingResponse(sse(), media_type="text/event-stream")

    async def approve(request: Request) -> JSONResponse:
        body = await request.json()
        session.decide(body["call_id"], body["decision"])
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[Route("/run", run, methods=["POST"]), Route("/approve", approve, methods=["POST"])]
    )


def build(workdir: str, fake: bool, provider_name: str) -> tuple[Agent, Session, EventLog]:
    """Create the agent, the session and the log."""
    if fake:
        provider = FakeProvider(
            [
                FakeProvider.call("write_file", {"file_path": "hello.txt", "content": "hi\n"}),
                FakeProvider.text("Wrote hello.txt."),
            ]
        )
        model = "fake"
    else:
        provider, model = providers.resolve(provider_name)
    workspace = Workspace(workdir)
    agent = Agent(provider, model, workspace=workspace, policy=Policy(workspace=workspace))
    log = EventLog(Path(workdir) / ".luna" / "example.db")
    return agent, Session.create(log, str(workspace.root)), log


async def self_test(workdir: str) -> None:
    """Drive the app in-process: run a turn, approve the write when it is asked for."""
    agent, session, log = build(workdir, True, "")
    transport = httpx.ASGITransport(app=make_app(agent, session))
    async with httpx.AsyncClient(transport=transport, base_url="http://app") as client:
        turn = asyncio.create_task(client.post("/run", json={"text": "write hello.txt"}))
        while not session.pending_approvals:  # the turn is now waiting for us
            await asyncio.sleep(0.01)
        (call_id,) = session.pending_approvals
        await client.post("/approve", json={"call_id": call_id, "decision": {"type": "approve"}})
        response = await turn
    events = [
        json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")
    ]
    print("events:", ", ".join(e["event"] for e in events))
    print("hello.txt:", (Path(workdir) / "hello.txt").read_text("utf-8").strip())
    log.close()


def main() -> None:
    """Serve the app, or run the self-test."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.self_test:
        asyncio.run(self_test(args.workdir))
        return
    import uvicorn

    try:
        agent, session, _log = build(args.workdir, False, args.provider)
    except LunaCoreError as exc:
        sys.exit(f"error: {exc}")
    uvicorn.run(make_app(agent, session), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
