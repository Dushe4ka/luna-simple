"""Transport-neutral slash-command types.

A handler never prints and never reads input: it returns a
:class:`CommandResult`. The REPL renders it to a console; the server
returns it as JSON and the TUI renders it as widgets. A question for the
user (a picker or a yes/no) is a ``choice`` / ``confirm`` whose answer is
simply the same command re-sent with an argument, so no surface has to
keep a pending question.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Protocol

from luna.turn.engine import Notice

if TYPE_CHECKING:
    from luna_core.background import AgentManager
    from luna_core.context import PinnedFiles
    from luna_core.loop import Agent
    from luna_core.session import Session

    from luna.config.config import LunaConfig
    from luna.core.agent import AgentBundle
    from luna.core.session_state import SessionState

#: ui = handled by the client; read = allowed mid-turn; mutate = changes the
#: session; prompt = becomes a message to the agent.
KINDS = ("ui", "read", "mutate", "prompt")

#: Commands that rewrite files and history: refused while background tasks run.
HISTORY_COMMANDS = frozenset({"/undo", "/redo", "/rewind"})


@dataclass(frozen=True)
class Choice:
    """Ask the user to pick one option, then re-send ``resubmit.format(value=...)``."""

    title: str
    options: list[tuple[str, str]]
    resubmit: str


@dataclass(frozen=True)
class Confirm:
    """Ask yes/no; on yes re-send ``resubmit``, on no show ``cancelled``."""

    question: str
    resubmit: str
    cancelled: str


@dataclass
class CommandResult:
    """Everything a command wants a client to show or do."""

    text: str = ""
    notices: list[Notice] = field(default_factory=list)
    choice: Choice | None = None
    confirm: Confirm | None = None
    prompt: str | None = None
    effects: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        """JSON shape of ``POST /sessions/{id}/command``."""
        return {
            "text": self.text,
            "notices": [asdict(n) for n in self.notices],
            "choice": (
                {
                    "title": self.choice.title,
                    "options": [list(o) for o in self.choice.options],
                    "resubmit": self.choice.resubmit,
                }
                if self.choice
                else None
            ),
            "confirm": asdict(self.confirm) if self.confirm else None,
            "prompt": self.prompt,
            "effects": self.effects,
        }


class CommandEnv(Protocol):
    """What a handler may use; see the REPL and server adapters."""

    workdir: str
    user_commands: dict
    pinned: PinnedFiles
    session: Session
    state: SessionState
    bundle: AgentBundle
    manager: AgentManager | None

    @property
    def config(self) -> LunaConfig:
        """Effective config (session overrides applied)."""

    @property
    def agent(self) -> Agent:
        """The session's current agent (``bundle.agent``)."""

    @property
    def can_rebuild(self) -> bool:
        """Whether this surface can rebuild the agent."""

    def live_tasks(self) -> bool:
        """Whether a background task of this session is queued or running."""

    def get_plan(self) -> bool | None:
        """Plan mode state, or ``None`` when plan mode is not available."""

    def set_plan(self, on: bool) -> None:
        """Turn plan mode on or off."""

    async def rebuild(self) -> None:
        """Rebuild the agent; raises on a bad config."""

    async def switch_model(self, model: str) -> None:
        """Switch model for this session; restores the old one and raises on failure."""

    async def switch_provider(self, provider: str) -> None:
        """Switch provider for this session; restores the old one and raises on failure."""


@dataclass(frozen=True)
class Command:
    """One registered slash command."""

    name: str
    help: str
    kind: str
    run: Callable[[CommandEnv, str], CommandResult | Awaitable[CommandResult]] | None
    #: the handler uses ``env.agent`` (the server builds the agent only for these)
    agent: bool = False
