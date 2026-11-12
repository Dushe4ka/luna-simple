"""Tests for ``luna doctor`` and ``luna update``."""

import subprocess
import sys

from luna_core import shell

from luna import doctor


def test_doctor_reports_each_area():
    names = {c.name for c in doctor.collect()}
    assert {"luna", "python", "path", "config", "shell", "git", "server"} <= names


def test_doctor_explains_a_missing_shell(monkeypatch):
    def missing(*a, **k):
        raise shell.ShellNotFound("нужен Git Bash: https://git-scm.com/download/win")

    monkeypatch.setattr(doctor, "find_shell", missing)
    check = next(c for c in doctor.collect() if c.name == "shell")
    assert not check.ok and "git-scm.com" in check.hint


def test_doctor_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "collect", lambda: [doctor.Check("x", False, "плохо", "сделай y")])
    assert doctor.run_doctor([]) == 1
    assert "сделай y" in capsys.readouterr().out


def _uv_dir(prefix):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, f"{prefix}\n", "")


def test_update_refuses_when_not_a_uv_tool(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    code = doctor.run_update(
        [], which=lambda n: "/bin/uv", run=_uv_dir(tmp_path / "tools"), windows=False
    )
    assert code == 1 and "uv tool" in capsys.readouterr().err


def test_update_runs_uv_tool_upgrade_on_posix(monkeypatch, tmp_path):
    tools = tmp_path / "tools"
    monkeypatch.setattr(sys, "prefix", str(tools / "luna-simple"))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{tools}\n" if "dir" in argv else "", "")

    assert doctor.run_update([], which=lambda n: "/bin/uv", run=run, windows=False) == 0
    assert calls[-1] == ["/bin/uv", "tool", "upgrade", "luna-simple"]


def test_update_on_windows_hands_off_to_a_new_console(monkeypatch, tmp_path):
    tools = tmp_path / "tools"
    monkeypatch.setattr(sys, "prefix", str(tools / "luna-simple"))
    started = []
    code = doctor.run_update(
        [],
        which=lambda n: r"C:\uv.exe",
        run=_uv_dir(tools),
        popen=lambda argv, **kw: started.append((argv, kw)),
        windows=True,
    )
    assert code == 0
    argv, kw = started[0]
    assert "upgrade" in " ".join(argv) and kw["creationflags"] & 0x00000010  # NEW_CONSOLE
