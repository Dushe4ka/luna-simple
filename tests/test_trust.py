import io

import pytest
from rich.console import Console

from luna.core.projects import ProjectIndex
from luna.ui.trust import REFUSED_MESSAGE, confirm_trust, ensure_trusted


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=100, force_terminal=False), buf


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "д", "да"])
def test_confirm_accepts_yes_answers(tmp_path, answer):
    console, buf = _console()
    assert confirm_trust(console, str(tmp_path), input_fn=lambda _p: answer) is True
    assert str(tmp_path) in buf.getvalue()


@pytest.mark.parametrize("answer", ["", "n", "нет", "maybe"])
def test_confirm_rejects_anything_else(tmp_path, answer):
    console, _ = _console()
    assert confirm_trust(console, str(tmp_path), input_fn=lambda _p: answer) is False


def test_ensure_trusted_records_trust_on_yes(tmp_path):
    console, _ = _console()
    assert ensure_trusted(console, str(tmp_path), input_fn=lambda _p: "y") is True
    assert ProjectIndex().is_trusted(str(tmp_path)) is True


def test_ensure_trusted_refusal_writes_nothing(tmp_path):
    console, buf = _console()
    assert ensure_trusted(console, str(tmp_path), input_fn=lambda _p: "n") is False
    assert ProjectIndex().list() == []
    assert REFUSED_MESSAGE in buf.getvalue()


@pytest.mark.parametrize("exc", [KeyboardInterrupt, EOFError])
def test_ctrl_c_or_eof_counts_as_refusal(tmp_path, exc):
    def boom(_prompt):
        raise exc

    console, buf = _console()
    assert ensure_trusted(console, str(tmp_path), input_fn=boom) is False
    assert ProjectIndex().list() == []
    assert REFUSED_MESSAGE in buf.getvalue()


def test_trusted_folder_skips_the_prompt(tmp_path):
    ProjectIndex().trust(str(tmp_path))

    def never(_prompt):
        pytest.fail("a trusted folder must not be asked about again")

    console, _ = _console()
    assert ensure_trusted(console, str(tmp_path), input_fn=never) is True


def test_prompt_shows_the_absolute_path_for_a_relative_workdir(tmp_path, monkeypatch):
    """Regression: the CLI passes `workdir="."`, and the prompt showed just "."."""
    monkeypatch.chdir(tmp_path)
    console, buf = _console()
    confirm_trust(console, ".", input_fn=lambda _p: "n")
    assert str(tmp_path.resolve()) in buf.getvalue()


def _ask_with_keys(keys: str):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from luna.ui.trust import trust_question

    with create_pipe_input() as inp:
        inp.send_text(keys)
        return trust_question(input=inp, output=DummyOutput()).unsafe_ask()


def test_escape_at_the_picker_counts_as_refusal():
    with pytest.raises(KeyboardInterrupt):
        _ask_with_keys("\x1b")


def test_enter_at_the_picker_accepts():
    assert _ask_with_keys("\r") == "yes"


def test_down_then_enter_refuses():
    assert _ask_with_keys("\x1b[B\r") == "no"


def test_unsaved_trust_is_reported_and_refused(tmp_path):
    """If the index cannot store trust, the server would 403 every request —
    say so up front instead of launching a TUI that cannot work."""

    class _BrokenIndex:
        def is_trusted(self, path):
            return False

        def trust(self, path):
            pass  # e.g. ~/.config/luna not writable

        def touch(self, path):
            pass

    console, buf = _console()
    ok = ensure_trusted(console, str(tmp_path), index=_BrokenIndex(), input_fn=lambda _p: "y")
    assert ok is False
    assert "Не удалось сохранить" in buf.getvalue()
