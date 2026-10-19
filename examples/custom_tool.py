"""A custom tool: ``word_count`` added next to the built-in tools.

Offline:   python examples/custom_tool.py --fake --workdir .
"""

import argparse
import asyncio
from pathlib import Path

from luna_core import (
    Agent,
    EventLog,
    Policy,
    Session,
    TextDelta,
    ToolContext,
    ToolFinished,
    ToolStarted,
    TurnDone,
    Workspace,
    builtin_tools,
    providers,
    tool,
)
from luna_core.providers.fake import FakeProvider


@tool(timeout=10)
def word_count(ctx: ToolContext, file_path: str) -> str:
    """Count the words in a text file.

    Args:
        ctx: Tool context, injected by the runtime and hidden from the schema.
        file_path: Path of the file, relative to the project root.

    """
    path = ctx.workspace.resolve(file_path)  # raises a tool error outside the project
    return f"{file_path}: {len(path.read_text('utf-8').split())} words"


async def main() -> None:
    """Run one turn that uses the custom tool."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fake", action="store_true", help="offline: scripted model")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("prompt", nargs="?", default="How many words are in notes.txt?")
    args = parser.parse_args()
    workdir = Path(args.workdir)
    if args.fake:
        (workdir / "notes.txt").write_text("one two three four\n", "utf-8")
        provider = FakeProvider(
            [
                FakeProvider.call("word_count", {"file_path": "notes.txt"}),
                FakeProvider.text("notes.txt has four words."),
            ]
        )
        model = "fake"
    else:
        provider, model = providers.resolve(args.provider)
    workspace = Workspace(workdir)
    agent = Agent(
        provider,
        model,
        tools=[*builtin_tools(), word_count],
        workspace=workspace,
        policy=Policy(workspace=workspace),
    )
    log = EventLog(workdir / ".luna" / "example.db")
    session = Session.create(log, str(workspace.root))
    async for event in agent.run(session, args.prompt):
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ToolStarted):
            print(f"[{event.name} {event.preview}]")
        elif isinstance(event, ToolFinished):
            print(f"  -> {event.detail}")
        elif isinstance(event, TurnDone):
            print()
    log.close()


if __name__ == "__main__":
    asyncio.run(main())
