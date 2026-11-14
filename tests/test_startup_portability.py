"""Startup portability: UTF-8 stdio and Ctrl+C without loop signal handlers."""

import asyncio
import signal

from luna import cli
from luna.core import session as session_mod


class _Stream:
    def __init__(self):
        self.calls = []

    def reconfigure(self, **kw):
        self.calls.append(kw)


def test_utf8_stdio_on_windows_only():
    out, err = _Stream(), _Stream()
    cli._utf8_stdio([out, err], windows=False)
    assert out.calls == []
    cli._utf8_stdio([out, err], windows=True)
    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}] == err.calls


async def test_ctrl_c_falls_back_to_a_plain_signal_handler(monkeypatch):
    loop = asyncio.get_running_loop()

    def unsupported(*a, **k):
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", unsupported)
    hits = []
    previous = signal.getsignal(signal.SIGINT)
    with session_mod._ctrl_c_cancels(lambda: hits.append(1)):
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(0.05)
    assert hits == [1]
    assert signal.getsignal(signal.SIGINT) is previous
