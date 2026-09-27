"""Claude-Code-style "do you trust this folder?" gate, shown before the TUI starts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from luna.core.projects import ProjectIndex
from luna.ui.interact import _real_terminal
from luna.ui.theme import PALETTE

REFUSED_MESSAGE = "Luna не запущена: папка не отмечена как доверенная."
_YES = {"y", "yes", "д", "да"}


def trust_question(**prompt_kwargs):
    """Build the "Да / Нет" picker with Esc bound to refusal.

    questionary's ``select`` only binds Ctrl+C to cancel; Esc did nothing and
    left the prompt hanging. Esc here exits with ``KeyboardInterrupt``, which
    :func:`ensure_trusted` treats exactly like "Нет". ``prompt_kwargs``
    (``input``/``output``) exist for tests.
    """
    import questionary
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
    from prompt_toolkit.keys import Keys

    question = questionary.select(
        "",
        choices=[
            questionary.Choice("Да, доверяю", value="yes"),
            questionary.Choice("Нет, выйти", value="no"),
        ],
        use_shortcuts=True,
        **prompt_kwargs,
    )
    escape = KeyBindings()

    @escape.add(Keys.Escape, eager=True)
    def _refuse(event) -> None:
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    app = question.application
    app.key_bindings = merge_key_bindings([app.key_bindings, escape])
    return question


def confirm_trust(console: Console, workdir: str, input_fn: Callable[[str], str] = input) -> bool:
    """Ask whether ``workdir`` is trusted; ``True`` only on an explicit yes.

    Uses the arrow-key picker in a real terminal and falls back to a plain
    ``[y/N]`` question otherwise.
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
    if not _real_terminal(console, input_fn):
        return input_fn("Доверяете? [y/N] ").strip().lower() in _YES
    console.print("Enter — подтвердить · Esc — выйти", style=PALETTE["moon_dim"])
    # unsafe_ask: let Ctrl+C / Esc propagate as KeyboardInterrupt without
    # questionary's own "Cancelled by user" line.
    return trust_question().unsafe_ask() == "yes"


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
