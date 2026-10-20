import re
from pathlib import Path

import luna_core

#: Frameworks and vendor SDKs the core must never import (spec: own adapters on httpx).
FORBIDDEN = (
    "deepagents",
    "langchain",
    "langgraph",
    "litellm",
    "openai",
    "anthropic",
    "google.genai",
    "google.generativeai",
)


def test_version():
    assert luna_core.__version__ == "0.1.0"


def test_core_never_imports_frameworks_or_vendor_sdks():
    root = Path(luna_core.__file__).parent
    names = "|".join(re.escape(m) for m in FORBIDDEN)
    pattern = re.compile(rf"^\s*(?:from|import)\s+(?:{names})\b", re.MULTILINE)
    offenders = [str(p) for p in root.rglob("*.py") if pattern.search(p.read_text("utf-8"))]
    assert offenders == []
