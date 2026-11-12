"""A tiny OpenAI-compatible chat server for CI: one execute call, then a text answer.

Streams the shapes ``luna_core.providers.openai_chat`` parses: ``choices[].delta``
with ``tool_calls`` / ``content``, a ``finish_reason``, a ``usage`` chunk, ``[DONE]``.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COMMAND = "echo привет && pwd"


def _chunks(messages: list[dict]) -> list[dict]:
    tool_results = [m for m in messages if m.get("role") == "tool"]
    if not tool_results:
        args = json.dumps({"command": COMMAND}, ensure_ascii=False)
        return [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "execute", "arguments": args},
                                }
                            ]
                        },
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ]
    content = tool_results[-1].get("content")
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return [
        {"choices": [{"index": 0, "delta": {"content": f"Готово. Вывод: {content}"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 7}},
    ]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep CI logs clean
        pass

    def do_GET(self):  # noqa: N802
        body = json.dumps({"data": [{"id": "mock"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in _chunks(request.get("messages", [])):
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    """Start the server on 127.0.0.1 in a daemon thread; return it and its port."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", required=True)
    args = parser.parse_args()
    srv, bound = serve()
    Path(args.port_file).write_text(str(bound), encoding="utf-8")
    threading.Event().wait()
