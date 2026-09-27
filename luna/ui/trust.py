"""Claude-Code-style "do you trust this folder?" gate, shown before the TUI starts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from luna.core.projects import ProjectIndex
from luna.ui.interact import arrow_pick
from luna.ui.theme import PALETTE

REFUSED_MESSAGE = "Luna не запущена: папка не отмечена как доверенная."
_YES = {"y", "yes", "д", "да"}


def confirm_trust(console: Console, workdir: str, input_fn: Callable[[str], str] = input) -> bool:
    """Ask whether ``workdir`` is trusted; ``True`` only on an explicit yes.

    Uses the arrow-key picker in a real terminal and falls back to a plain
    ``[y/N]`` question otherwise (``arrow_pick`` returns ``None`` there).
    Esc / Ctrl+C propagate as ``KeyboardInterrupt`` for the caller to treat
    as a refusal.
    """
    console.print()
    console.print("[bold]Доверяете этой папке?[/]")
    console.print()
    # soft_wrap: the full path must be visible — never ellipsis-truncated.
    # Resolved: the CLI may pass a relative "." — the user must see the real folder.
    shown = str(Path(workdir).resolve())
    console.print(shown, style=PALETTE["peri"], highlight=False, soft_wrap=True)
    console.print()
    console.print(
        "Luna сможет читать и изменять файлы в этой папке и запускать в ней\n"
        "команды. Открывайте только проекты, которым доверяете.",
        style=PALETTE["moon_dim"],
    )
    console.print()
    choice = arrow_pick(console, input_fn, [("yes", "Да, доверяю"), ("no", "Нет, выйти")])
    if choice is None:
        return input_fn("Доверяете? [y/N] ").strip().lower() in _YES
    return choice == "yes"


def ensure_trusted(
    console: Console,
    workdir: str,
    *,
    index: ProjectIndex | None = None,
    input_fn: Callable[[str], str] = input,
) -> bool:
    """Return whether Luna may run in ``workdir``, prompting once if unknown.

    A trusted folder is only touched (``last_opened``); an unknown one is
    asked about and recorded on yes. A refusal — including Esc, Ctrl+C or a
    closed stdin — writes nothing and prints :data:`REFUSED_MESSAGE`.
    """
    index = index or ProjectIndex()
    if index.is_trusted(workdir):
        index.touch(workdir)
        return True
    try:
        trusted = confirm_trust(console, workdir, input_fn)
    except (KeyboardInterrupt, EOFError):
        console.print()
        trusted = False
    if trusted:
        index.trust(workdir)
    else:
        console.print(REFUSED_MESSAGE)
    return trusted
