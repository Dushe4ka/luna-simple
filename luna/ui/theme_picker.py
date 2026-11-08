"""Arrow-key theme picker with a live preview painted in the highlighted theme."""

from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.style import Style
from rich.text import Text

from luna.ui.diffview import render_diff
from luna.ui.themes import AUTO, AUTO_TITLE, ThemeSpec, available_themes, resolve, rich_color
from luna.ui.themes import rich_theme as build_rich_theme

TITLE = "Выберите тему оформления"
SUBTITLE = "Сменить потом: /theme или luna config set ui.theme …"
HINT = "↑↓ выбрать · Enter сохранить · Esc — Авто"
PREVIEW_WIDTH = 60
_ESC_TIMEOUT = 0.05
_SAMPLE_DIFF = "@@ -1,2 +1,2 @@\n-Luna — простой агнет\n+Luna — простой агент\n для кода"


def theme_options(dark_terminal: bool) -> list[tuple[str, str]]:
    """``(id, label)`` pairs: ``auto`` (with what it resolves to now), then every theme."""
    resolved = resolve(AUTO, dark_terminal=dark_terminal)
    themes = available_themes()
    return [(AUTO, f"{AUTO_TITLE} — сейчас: {resolved.title}"), *((t.id, t.title) for t in themes)]


def _sample_lines() -> list[Text]:
    """Build the preview content: own message, a finished tool, a diff, an answer."""
    message = Text.assemble(("┃ ", "luna.primary"), ("Исправь опечатку в README", "luna.strong"))
    tool = Text.assemble(
        ("  ✓ ", "luna.ok"), ("edit_file", "luna.tool.name"), (" README.md", "luna.muted")
    )
    diff = [Text("  ") + line for line in render_diff(_SAMPLE_DIFF).split("\n")]
    answer = Text.assemble(
        ("┃ ", "luna.accent"),
        ("Готово: ", "luna.text"),
        ("агнет", "luna.code"),
        (" → ", "luna.text"),
        ("агент", "luna.code"),
    )
    return [Text(""), message, tool, *diff, answer, Text("")]


def render_preview(spec: ThemeSpec, width: int = PREVIEW_WIDTH) -> str:
    """Render the sample in ``spec`` as an ANSI string, each line filled with its ``bg``."""
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="standard" if spec.ansi else "truecolor",
        width=width,
        theme=build_rich_theme(spec),
        legacy_windows=False,
    )
    base = Style(color=rich_color(spec.tokens["text"]), bgcolor=rich_color(spec.tokens["bg"]))
    for line in _sample_lines():
        line.style = base
        line.truncate(width)
        line.align("left", width)
        console.print(line, end="\n", no_wrap=True, overflow="crop", soft_wrap=False)
    return buf.getvalue().rstrip("\n")


def _header() -> str:
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, color_system="standard", width=200)
    console.print(Text(TITLE, style="bold"))
    console.print(Text(SUBTITLE, style="dim"))
    return buf.getvalue()


def _option_lines(options: list[tuple[str, str]], index: int) -> str:
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, color_system="standard", width=200)
    for i, (_, label) in enumerate(options):
        if i == index:
            console.print(Text(f"❯ {label}", style="bold reverse"))
        else:
            console.print(Text(f"  {label}"))
    return buf.getvalue()


def pick_theme(*, current: str, dark_terminal: bool, input=None, output=None) -> str | None:  # noqa: A002
    """Show the picker; return the chosen theme id.

    ↑/↓ move and repaint the preview, Enter saves the highlighted theme,
    Esc means ``auto``, Ctrl+C raises ``KeyboardInterrupt``. The picker is
    erased from the screen when done. ``input``/``output`` are for tests.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    options = theme_options(dark_terminal)
    ids = [option_id for option_id, _ in options]
    specs = {t.id: t for t in available_themes()}
    state = {"index": ids.index(current) if current in ids else 0}

    def spec_at(index: int) -> ThemeSpec:
        option_id = ids[index]
        if option_id == AUTO:
            return resolve(AUTO, dark_terminal=dark_terminal)
        return specs[option_id]

    def text() -> ANSI:
        index = state["index"]
        width = PREVIEW_WIDTH
        app = app_ref.get("app")
        if app is not None:
            width = max(20, min(PREVIEW_WIDTH, app.output.get_size().columns - 1))
        body = (
            _header()
            + "\n"
            + _option_lines(options, index)
            + "\n"
            + render_preview(spec_at(index), width)
            + "\n\n"
            + HINT
        )
        return ANSI(body)

    kb = KeyBindings()

    @kb.add("up")
    def _up(event) -> None:
        state["index"] = max(0, state["index"] - 1)

    @kb.add("down")
    def _down(event) -> None:
        state["index"] = min(len(ids) - 1, state["index"] + 1)

    @kb.add("enter")
    def _enter(event) -> None:
        event.app.exit(result=ids[state["index"]])

    @kb.add("escape")
    def _escape(event) -> None:
        event.app.exit(result=AUTO)

    @kb.add("c-c")
    def _interrupt(event) -> None:
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    app_ref: dict = {}
    app: Application[str] = Application(
        layout=Layout(Window(FormattedTextControl(text), always_hide_cursor=True)),
        key_bindings=kb,
        full_screen=False,
        erase_when_done=True,
        input=input,
        output=output,
    )
    app.ttimeoutlen = _ESC_TIMEOUT
    app_ref["app"] = app
    return app.run()
