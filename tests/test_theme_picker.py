"""Theme picker: options, live preview, key handling (offline, no real terminal)."""

import threading

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from luna.ui.theme_picker import pick_theme, render_preview, theme_options
from luna.ui.themes import available_themes, load_theme

_TIMEOUT = 10


def _pick(keys, *, current="auto", **kw):
    """Run the picker on a pipe in a thread, so a stuck key never hangs the suite."""
    box = {}

    def run():
        with create_pipe_input() as inp:
            inp.send_text(keys)
            with create_app_session(input=inp, output=DummyOutput()):
                try:
                    box["value"] = pick_theme(
                        current=current, dark_terminal=True, input=inp, output=DummyOutput(), **kw
                    )
                except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
                    box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(_TIMEOUT)
    assert not worker.is_alive(), "picker did not finish"
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_enter_on_first_is_auto():
    assert _pick("\r") == "auto"


def test_arrow_down_picks_moonlight_then_daymoon():
    assert _pick("\x1b[B\r") == "moonlight"
    assert _pick("\x1b[B\x1b[B\r") == "daymoon"


def test_arrow_up_does_not_go_above_the_first():
    assert _pick("\x1b[A\x1b[A\r") == "auto"


def test_escape_means_auto():
    assert _pick("\x1b[B\x1b[B\x1b") == "auto"


def test_ctrl_c_raises_keyboard_interrupt():
    with pytest.raises(KeyboardInterrupt):
        _pick("\x1b[B\x03")


def test_cursor_starts_on_current():
    assert _pick("\r", current="daymoon") == "daymoon"
    assert _pick("\x1b[B\r", current="daymoon") == "moonlight-cb"


def test_options_show_resolved_auto():
    opts = theme_options(dark_terminal=False)
    assert opts[0][0] == "auto" and "Дневная луна" in opts[0][1]
    assert [i for i, _ in opts[1:]] == [t.id for t in available_themes()]


def test_preview_paints_theme_background():
    out = render_preview(load_theme("daymoon"), width=40)
    assert "48;2;248;247;243" in out  # bg #f8f7f3 as truecolor background
    assert "✓" in out and "+" in out and "-" in out


def test_preview_lines_are_padded_to_width():
    from rich.text import Text

    out = render_preview(load_theme("moonlight"), width=40)
    lines = out.split("\n")
    assert len(lines) >= 7
    for line in lines:
        assert Text.from_ansi(line).cell_len == 40


def test_preview_renders_ansi_themes():
    out = render_preview(load_theme("ansi-dark"), width=40)
    assert "✓" in out


def test_escape_value_keeps_the_given_theme():
    assert _pick("\x1b[B\x1b", current="daymoon", escape_value="daymoon") == "daymoon"
