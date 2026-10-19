"""Minimal embedded agent: one turn in a folder, printing the streamed answer.

Offline:   python examples/minimal_agent.py --fake --workdir .
Real:      ANTHROPIC_API_KEY=... python examples/minimal_agent.py "What is this project?"
"""

import argparse
import asyncio
import sys
from pathlib import Path

from luna_core import (
    Agent,
    Decision,
    EventLog,
    LunaCoreError,
    NeedsApproval,
    Policy,
    Session,
    TextDelta,
    ToolStarted,
    TurnDone,
    TurnError,
    Workspace,
    providers,
)
from luna_core.providers.fake import FakeProvider


async def main() -> None:
    """Run one turn and print the events."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fake", action="store_true", help="offline: scripted model")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument(
        "prompt", nargs="?", default="List the files here and say what this project is."
    )
    args = parser.parse_args()
    if args.fake:
        provider = FakeProvider(
            [FakeProvider.call("ls", {"path": "."}), FakeProvider.text("This folder is a project.")]
        )
        model = "fake"
    else:
        provider, model = providers.resolve(args.provider)
    workspace = Workspace(args.workdir)
    agent = Agent(provider, model, workspace=workspace, policy=Policy(workspace=workspace))
    log = EventLog(Path(args.workdir) / ".luna" / "example.db")
    session = Session.create(log, str(workspace.root))
    async for event in agent.run(session, args.prompt):
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ToolStarted):
            print(f"\n[{event.name} {event.preview}]")
        elif isinstance(event, NeedsApproval):
            # A turn that needs approval waits until somebody answers.
            session.decide(event.request.call_id, Decision.reject("this example is read-only"))
        elif isinstance(event, TurnDone):
            print(f"\n\n{event.usage.input + event.usage.output} tokens")
        elif isinstance(event, TurnError):
            print(f"\nerror: {event.message}")
    log.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except LunaCoreError as exc:
        sys.exit(f"error: {exc}")
