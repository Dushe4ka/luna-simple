import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "ci"))
import mock_llm  # noqa: E402


def _post(port, messages):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps({"messages": messages}).encode(),
        headers={"Content-Type": "application/json"},
    )
    raw = urllib.request.urlopen(req, timeout=5).read().decode("utf-8")
    return [line[6:] for line in raw.splitlines() if line.startswith("data: ")]


def test_mock_calls_execute_then_answers():
    server, port = mock_llm.serve()
    try:
        first = _post(port, [{"role": "user", "content": "go"}])
        assert first[-1] == "[DONE]" and '"execute"' in first[0]
        second = _post(port, [{"role": "tool", "content": "привет"}])
        assert "привет" in json.loads(second[0])["choices"][0]["delta"]["content"]
    finally:
        server.shutdown()
