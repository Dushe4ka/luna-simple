"""The SDK examples run offline (scripted model, no network)."""

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("acp")
pytest.importorskip("starlette")

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

CASES = [
    ("minimal_agent.py", "--fake", "This folder is a project."),
    ("custom_tool.py", "--fake", "four words"),
    ("custom_provider.py", "--fake", "You said: hello from a custom provider"),
    ("remote_client.py", "--fake", "Hello from the fake model."),
    ("approvals_web.py", "--self-test", "hello.txt: hi"),
    ("acp_agent.py", "--self-test", "stop reason: end_turn"),
]


@pytest.mark.parametrize(("name", "flag", "expected"), CASES, ids=[c[0] for c in CASES])
def test_example_runs_offline(name, flag, expected, tmp_path):
    out = subprocess.run(
        [sys.executable, str(EXAMPLES / name), flag, "--workdir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert out.returncode == 0, out.stderr
    assert expected in out.stdout, out.stdout
