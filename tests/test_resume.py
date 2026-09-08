"""Tests for ``luna.cli._resolve_resume`` — ``-c`` / ``--resume`` handling."""

import argparse
import io

from rich.console import Console

from luna.cli import _resolve_resume
from luna.persistence import SessionIndex


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=True)


def _args(cont: bool = False, resume: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(cont=cont, resume=resume)


def test_continue_returns_recorded_thread(tmp_path):
    idx = SessionIndex()
    idx.record("thread-a", str(tmp_path), "a task")
    got = _resolve_resume(_args(cont=True), idx, str(tmp_path), _console(), interactive=False)
    assert got == "thread-a"


def test_resume_numeric_is_a_list_index(tmp_path):
    idx = SessionIndex()
    idx.record("thread-old", str(tmp_path), "older")
    idx.record("thread-new", str(tmp_path), "newer")
    got = _resolve_resume(_args(resume="1"), idx, str(tmp_path), _console(), interactive=False)
    assert got == "thread-new"  # newest first → [1]


def test_resume_non_digit_is_treated_as_raw_thread_id(tmp_path):
    idx = SessionIndex()
    got = _resolve_resume(_args(resume="nope"), idx, str(tmp_path), _console(), interactive=False)
    assert got == "nope"


def test_resume_list_non_interactive_returns_none(tmp_path):
    idx = SessionIndex()
    idx.record("thread-a", str(tmp_path), "a task")
    got = _resolve_resume(
        _args(resume="__list__"), idx, str(tmp_path), _console(), interactive=False
    )
    assert got is None
