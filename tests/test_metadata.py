import subprocess
import sys

import luna


def test_version_constant():
    assert luna.__version__ == "0.2.0"


def test_module_entrypoint_runs():
    out = subprocess.run(
        [sys.executable, "-m", "luna", "--version"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0
    assert "0.2.0" in out.stdout
