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
