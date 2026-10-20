"""A custom provider: any object with ``name`` and ``async stream(request)``.

This one echoes the last user message. ``--fake`` is accepted for uniformity with the
other examples; this example never touches the network either way.
"""

import argparse
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from luna_core import Agent, EventLog, Policy, Session, TextDelta, TurnDone, Usage, Workspace
from luna_core.events import StopEvent, UsageReport
from luna_core.providers import ModelRequest


class EchoProvider:
    """Replies with the last user message, word by word."""

    name = "echo"

    async def stream(self, request: ModelRequest) -> AsyncIterator[object]:
        """Yield model events: text deltas, a usage report and a stop event."""
        last = request.messages[-1].text
        for word in f"You said: {last}".split(" "):
            yield TextDelta(word + " ")
        yield UsageReport(Usage(input=len(last.split()), output=4))
        yield StopEvent("end")


async def main() -> None:
    """Run one turn against the echo provider."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fake", action="store_true", help="accepted, has no effect")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("prompt", nargs="?", default="hello from a custom provider")
    args = parser.parse_args()
    workspace = Workspace(args.workdir)
    agent = Agent(
        EchoProvider(), "echo-1", tools=[], workspace=workspace, policy=Policy(workspace=workspace)
    )
    log = EventLog(Path(args.workdir) / ".luna" / "example.db")
    session = Session.create(log, str(workspace.root))
    async for event in agent.run(session, args.prompt):
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, TurnDone):
            print(f"\n({event.stop_reason})")
    log.close()


if __name__ == "__main__":
    asyncio.run(main())
