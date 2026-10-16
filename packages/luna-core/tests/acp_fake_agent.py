"""An ACP agent process backed by FakeProvider (for test_acp)."""

import asyncio
import sys
from pathlib import Path

from luna_core.acp import run_acp
from luna_core.loop import Agent
from luna_core.policy import Policy
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog
from luna_core.workspace import Workspace


def make_agent(cwd: str) -> Agent:
    script = [
        FakeProvider.call("execute", {"command": "echo hi"}, "c1"),
        FakeProvider.text("done: hi"),
    ]
    ws = Workspace(cwd)
    return Agent(FakeProvider(script), "fake", workspace=ws, policy=Policy(workspace=ws))


if __name__ == "__main__":
    asyncio.run(run_acp(make_agent, EventLog(Path(sys.argv[1]) / "acp.db")))
