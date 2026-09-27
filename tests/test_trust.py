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
