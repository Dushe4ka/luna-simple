"""Static checks of the one-line installers (no network, no PowerShell needed)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PINS = {
    "PortableGit-2.56.0.4-64-bit.7z.exe": (
        "b1f3e52216e2ed6882d1bebfd4d3fdf319bec40d2edbbf9fd26ec4fd3fce7ed1"
    ),
    "PortableGit-2.56.0.4-arm64.7z.exe": (
        "044110f9d951858452ddb5e4b5415ea0f2b5b2598e794d6fec77fe28b9aa8cc0"
    ),
}


def test_install_sh_is_valid_posix_sh():
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh")
    assert subprocess.run([sh, "-n", str(SCRIPTS / "install.sh")], check=False).returncode == 0


def test_both_installers_install_the_same_package():
    spec = "luna-simple[all] @ git+"
    assert spec in (SCRIPTS / "install.sh").read_text(encoding="utf-8")
    assert spec in (SCRIPTS / "install.ps1").read_text(encoding="utf-8")


def test_portable_git_is_pinned_by_sha256():
    text = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
    for name, digest in PINS.items():
        assert name in text
        assert digest in text


def test_ps1_never_accepts_the_wsl_bash():
    text = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
    assert re.search(r"windowsapps", text, re.IGNORECASE)
    assert "SystemRoot" in text


def test_install_sh_runs_main_as_its_last_line():
    lines = [
        x for x in (SCRIPTS / "install.sh").read_text(encoding="utf-8").splitlines() if x.strip()
    ]
    assert lines[-1] == 'main "$@"'


def test_ps1_die_throws_instead_of_exiting():
    text = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
    die = next(x for x in text.splitlines() if x.strip().startswith("function Die"))
    assert "throw" in die
    assert "exit" not in die


def test_readme_shows_how_to_pin_a_ref():
    text = (SCRIPTS.parent / "README.md").read_text(encoding="utf-8")
    assert "| LUNA_REF=" in text
    assert "$env:LUNA_REF" in text


def test_ps1_is_ascii_only_so_windows_powershell_5_parses_it():
    """Windows PowerShell 5.1 reads a BOM-less script file in the ANSI code page:
    the UTF-8 bytes of ``—`` and ``ё`` decode to quote characters there and break
    parsing (final review minor 7). ASCII also survives ``irm | iex`` unchanged."""
    data = (SCRIPTS / "install.ps1").read_bytes()
    bad = [n for n, line in enumerate(data.splitlines(), 1) if any(b > 0x7F for b in line)]
    assert bad == []
