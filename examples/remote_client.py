"""Talk to a Luna server with ``LunaClient`` (REST + SSE, protocol v1).

Against a running server (start one with ``LUNA_SERVER_TOKEN=SECRET luna serve --port 8765``;
the token goes through the environment, not argv, where ``ps`` would show it):
    LUNA_SERVER_TOKEN=SECRET python examples/remote_client.py --url http://127.0.0.1:8765

Offline: ``--fake`` runs the server app in-process with a scripted model.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx
from luna_core import LunaCoreError
from luna_core.client import LunaClient


async def converse(client: LunaClient, workdir: str, prompt: str) -> None:
    """Create a session, send one message and print the streamed answer."""
    health = await client.health()
    print(f"server {health['version']}, protocol {health['protocol']}")
    session_id = await client.create_session(workdir)
    async for event in client.send(session_id, prompt, workdir):
        if event["event"] == "text_delta":
            print(event["text"], end="", flush=True)
        elif event["event"] == "approval_needed":
            # Unanswered approvals keep the turn waiting; reject to stay read-only.
            call_id = event["value"]["action_requests"][0]["call_id"]
            await client.decide(session_id, call_id, {"type": "reject", "message": "no"})
        elif event["event"] == "turn_done":
            print()
    print("sessions:", len(await client.list_sessions(workdir)))


async def main() -> None:
    """Parse arguments and run the conversation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fake", action="store_true", help="offline: in-process server")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--token",
        default=os.environ.get("LUNA_SERVER_TOKEN", ""),
        help="bearer token (default: $LUNA_SERVER_TOKEN)",
    )
    parser.add_argument("--workdir", default=".")
    parser.add_argument("prompt", nargs="?", default="Say hello.")
    args = parser.parse_args()
    workdir = str(Path(args.workdir).resolve())
    if not args.fake:
        client = LunaClient(args.url, args.token)
        try:
            await converse(client, workdir, args.prompt)
        finally:
            await client.aclose()
        return
    # Keep the in-process server away from the user's real config and sessions.
    os.environ["XDG_CONFIG_HOME"] = str(Path(workdir) / ".luna" / "config")
    from luna_core.providers.fake import FakeProvider
    from luna_core.session import EventLog

    from luna.server.app import create_app
    from luna.server.run import make_session_build

    app = create_app(
        token="example",
        build=make_session_build(
            provider_factory=lambda runtime: FakeProvider(
                [FakeProvider.text("Hello from the fake model.")]
            )
        ),
        trust_check=lambda path: True,
        log=EventLog(Path(workdir) / ".luna" / "example.db"),
    )
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://luna")
    client = LunaClient("http://luna", "example", client=http)
    try:
        await converse(client, workdir, args.prompt)
    finally:
        await client.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except LunaCoreError as exc:
        sys.exit(
            f"error: {exc}\nstart a server with: LUNA_SERVER_TOKEN=SECRET luna serve --port 8765 "
            "(then pass --url with the same LUNA_SERVER_TOKEN), or use --fake"
        )
