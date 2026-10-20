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


@pytest.mark.parametrize(
    "name",
    ["minimal_agent.py", "custom_tool.py", "approvals_web.py", "remote_client.py", "acp_agent.py"],
)
def test_example_without_key_or_server_fails_cleanly(name, tmp_path):
    import os

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.endswith(("_API_KEY", "_CREDENTIALS", "_FOLDER_ID"))
    }
    env.update(HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"))
    args = ["--url", "http://127.0.0.1:9", "--token", "x"] if name == "remote_client.py" else []
    out = subprocess.run(
        [sys.executable, str(EXAMPLES / name), "--workdir", str(tmp_path), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    assert out.returncode != 0
    assert "Traceback" not in out.stderr, out.stderr
    assert "error:" in out.stderr
