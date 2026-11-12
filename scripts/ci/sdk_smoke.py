"""Run with a Python that has ONLY luna-core installed (no app): SDK examples + LocalSandbox."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
# examples that need only luna-core (approvals_web/acp_agent/remote_client need extras or the app)
CASES = [
    ("minimal_agent.py", "This folder is a project."),
    ("custom_tool.py", "four words"),
    ("custom_provider.py", "You said: hello from a custom provider"),
]


def main() -> None:
    """Run the SDK examples and LocalSandbox; exit 1 on failure."""
    from luna_core import LocalSandbox

    work = Path(tempfile.mkdtemp(prefix="luna-sdk-"))
    for name, expected in CASES:
        out = subprocess.run(
            [sys.executable, str(EXAMPLES / name), "--fake", "--workdir", str(work)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            cwd=work,
        )
        if out.returncode or expected not in out.stdout:
            print(f"SDK SMOKE FAIL {name}:\n{out.stdout}\n{out.stderr}")
            sys.exit(1)
    cwd = work / "Мой проект"
    cwd.mkdir()
    result = asyncio.run(LocalSandbox().run("echo привет && pwd", cwd=cwd, timeout=60))
    if result.exit_code or "привет" not in result.output:
        print(f"SDK SMOKE FAIL LocalSandbox: {result}")
        sys.exit(1)
    print("SDK SMOKE OK")


if __name__ == "__main__":
    main()
