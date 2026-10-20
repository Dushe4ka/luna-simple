"""Serve Luna to an editor over ACP (stdio).

Editor config (Zed, ``settings.json``):
    "agent_servers": {"Luna": {"command": "python", "args": ["examples/acp_agent.py"]}}

Needs the ``acp`` extra: ``pip install "luna-core[acp]"``.
Self-test: ``python examples/acp_agent.py --self-test`` plays an editor against this file.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from luna_core import Agent, EventLog, LunaCoreError, Policy, Workspace, providers
from luna_core.acp import run_acp
from luna_core.providers.fake import FakeProvider


def make_agent_factory(fake: bool, provider_name: str):
    """Return ``make_agent(cwd)``: ACP builds one agent per editor session folder."""

    def make_agent(cwd: str) -> Agent:
        if fake:
            provider = FakeProvider(
                [
                    FakeProvider.call("execute", {"command": "echo hi"}),
                    FakeProvider.text("done: hi"),
                ]
            )
            model = "fake"
        else:
            provider, model = providers.resolve(provider_name)
        workspace = Workspace(cwd)
        return Agent(provider, model, workspace=workspace, policy=Policy(workspace=workspace))

    return make_agent


async def self_test(workdir: str) -> None:
    """Play an ACP client: initialize, open a session, prompt, grant the permission."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, __file__, "--fake", "--workdir", workdir,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )  # fmt: skip

    async def send(obj: dict) -> None:
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        await proc.stdin.drain()

    async def read() -> dict:
        return json.loads(await asyncio.wait_for(proc.stdout.readline(), 20))

    try:
        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": 1, "clientCapabilities": {}}})  # fmt: skip
        print("protocol:", (await read())["result"]["protocolVersion"])
        await send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
                    "params": {"cwd": workdir, "mcpServers": []}})  # fmt: skip
        session_id = (await read())["result"]["sessionId"]
        await send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                    "params": {"sessionId": session_id,
                               "prompt": [{"type": "text", "text": "run it"}]}})  # fmt: skip
        kinds, final = [], None
        while final is None:
            msg = await read()
            if msg.get("method") == "session/request_permission":
                await send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "outcome": {"outcome": "selected", "optionId": "allow_once"}}})  # fmt: skip
            elif msg.get("method") == "session/update":
                kinds.append(msg["params"]["update"]["sessionUpdate"])
            elif msg.get("id") == 3:
                final = msg
        print("updates:", ", ".join(dict.fromkeys(kinds)))
        print("stop reason:", final["result"]["stopReason"])
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except TimeoutError:
            proc.kill()


def main() -> None:
    """Serve ACP on stdio, or run the self-test."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--fake", action="store_true", help="offline: scripted model")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--provider", default="anthropic")
    args = parser.parse_args()
    if args.self_test:
        asyncio.run(self_test(str(Path(args.workdir).resolve())))
        return
    if sys.stdin.isatty():
        sys.exit(
            "error: ACP speaks JSON-RPC on stdio; start this from an editor (or use --self-test)"
        )
    make_agent = make_agent_factory(args.fake, args.provider)
    try:
        make_agent(str(Path(args.workdir).resolve()))  # fail fast on a missing key
    except LunaCoreError as exc:
        sys.exit(f"error: {exc}")
    log = EventLog(Path(args.workdir) / ".luna" / "acp.db")
    asyncio.run(run_acp(make_agent, log))


if __name__ == "__main__":
    main()
