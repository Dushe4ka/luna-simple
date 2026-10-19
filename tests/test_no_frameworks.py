"""The app and the core run without deepagents / LangChain / LangGraph / litellm."""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(
    r"^\s*(from|import)\s+(deepagents|langchain\w*|langgraph\w*|litellm)\b", re.M
)
FRAMEWORKS = ("deepagents", "langchain", "langchain_core", "langgraph")


def test_no_framework_imports_in_the_repo():
    offenders = [
        str(p.relative_to(ROOT))
        for base in ("luna", "packages", "tests")
        for p in (ROOT / base).rglob("*.py")
        if FORBIDDEN.search(p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == []


def test_frameworks_are_not_installed():
    assert [name for name in FRAMEWORKS if importlib.util.find_spec(name)] == []


def test_importing_the_cli_loads_no_framework():
    code = (
        "import sys, luna.cli; "
        f"print(sorted(m for m in sys.modules if m.split('.')[0] in {FRAMEWORKS!r}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "[]"
