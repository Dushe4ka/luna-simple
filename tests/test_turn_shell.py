"""fmt / diagnose / verify / ``!cmd`` run through ``luna_core.shell``."""

import pytest
from luna_core import shell

from luna.repl import usercmd
from luna.turn import diagnose, fmt, verify


@pytest.fixture
def no_shell(monkeypatch):
    def missing(command, **kw):
        raise shell.ShellNotFound("нужен Git Bash: https://git-scm.com/download/win")

    for mod in (fmt, diagnose, verify, usercmd):
        monkeypatch.setattr(mod, "run_sync", missing)


def test_verify_runs_through_the_shell_and_reads_utf8(tmp_path):
    ok, tail = verify.run_verify("echo привет && exit 0", str(tmp_path))
    assert ok and "привет" in tail


def test_verify_without_shell_fails_with_hint(tmp_path, no_shell):
    ok, tail = verify.run_verify("echo hi", str(tmp_path))
    assert not ok and "git-scm.com" in tail


def test_fmt_and_diagnose_degrade_without_shell(tmp_path, no_shell, monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda name: "/x/" + name)
    assert fmt.run("ruff format", str(tmp_path), ["a.py"]) == []
    assert diagnose.run("ruff check", str(tmp_path), ["a.py"]) == ""


def test_usercmd_shell_injection_uses_the_shell(tmp_path):
    cmd = usercmd.UserCommand(name="x", description="", body="v=!`echo привет`")
    assert "привет" in usercmd.expand(cmd, "", str(tmp_path))


def test_usercmd_without_shell_is_empty(tmp_path, no_shell):
    cmd = usercmd.UserCommand(name="x", description="", body="v=!`echo hi`")
    assert usercmd.expand(cmd, "", str(tmp_path)) == "v="
