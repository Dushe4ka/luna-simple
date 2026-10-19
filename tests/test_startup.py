"""``luna --version`` stays instant: nothing heavy is imported on that path."""

import os
import statistics
import subprocess
import sys
import time

import pytest

HEAVY = ("textual", "rich", "httpx", "luna_core", "starlette")


def _median_seconds(args: list[str]) -> float:
    runs = []
    for _ in range(5):
        start = time.perf_counter()
        subprocess.run([sys.executable, *args], capture_output=True, check=True)
        runs.append(time.perf_counter() - start)
    return statistics.median(runs)


@pytest.mark.skipif(bool(os.environ.get("CI")), reason="timing is not stable on shared runners")
def test_version_is_fast():
    # The target is ~150 ms on a laptop; the generous bound only catches regressions
    # such as a heavy import creeping into the --version path.
    assert _median_seconds(["-m", "luna", "--version"]) < 0.5


def test_version_path_imports_nothing_heavy():
    code = (
        "import sys, luna.cli as c; assert c.main(['--version']) == 0\n"
        f"print(sorted(m for m in sys.modules if m.split('.')[0] in {HEAVY!r}))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.stdout.strip().splitlines()[-1] == "[]", result.stderr
