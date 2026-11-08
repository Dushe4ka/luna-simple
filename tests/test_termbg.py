"""Tests for terminal background detection."""

import sys
import types

from luna.ui import termbg


def test_parse_osc11_dark_and_light():
    assert termbg.parse_osc11("\x1b]11;rgb:0e0e/1111/1717\x07") is True
    assert termbg.parse_osc11("\x1b]11;rgb:f8f8/f7f7/f3f3\x1b\\") is False
    assert termbg.parse_osc11("\x1b]11;rgb:ff/ff/ff\x07\x1b[?62;22c") is False


def test_parse_osc11_garbage():
    assert termbg.parse_osc11("") is None
    assert termbg.parse_osc11("\x1b[?62c") is None


def test_parse_colorfgbg():
    assert termbg.parse_colorfgbg("15;0") is True
    assert termbg.parse_colorfgbg("0;15") is False
    assert termbg.parse_colorfgbg("0;default;7") is False
    assert termbg.parse_colorfgbg(None) is None
    assert termbg.parse_colorfgbg("x") is None


def test_detect_prefers_osc():
    termbg.reset_cache()
    reply = "\x1b]11;rgb:ffff/ffff/ffff\x07"
    assert termbg.detect_dark({"COLORFGBG": "15;0"}, query=lambda: reply) is False
    termbg.reset_cache()


def test_detect_falls_back():
    termbg.reset_cache()
    assert termbg.detect_dark({"COLORFGBG": "0;15"}, query=lambda: None) is False
    termbg.reset_cache()
    assert termbg.detect_dark({}, query=lambda: None) is True
    termbg.reset_cache()


def test_query_without_tty_returns_none(monkeypatch):
    fake = types.SimpleNamespace(isatty=lambda: False)
    monkeypatch.setattr(sys, "stdin", fake)
    assert termbg.query_osc11() is None


def test_query_reads_until_da1(monkeypatch):
    import os
    import select
    import termios
    import tty

    fake = types.SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(sys, "stdin", fake)
    monkeypatch.setattr(sys, "stdout", fake)
    chunks = [b"\x1b]11;rgb:0/0/0\x07", b"\x1b[?62;22c"]
    restored = []
    written = []
    closed = []
    monkeypatch.setattr(os, "open", lambda *a, **k: 99)
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(os, "write", lambda fd, data: written.append(data))
    monkeypatch.setattr(os, "read", lambda fd, n: chunks.pop(0))
    monkeypatch.setattr(select, "select", lambda r, w, x, t: (r, [], []))
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["old"])
    monkeypatch.setattr(termios, "tcsetattr", lambda *a: restored.append(a))
    monkeypatch.setattr(tty, "setraw", lambda fd: None)

    reply = termbg.query_osc11()

    assert reply is not None
    assert "rgb:0/0/0" in reply and "[?62;22c" in reply
    assert restored and restored[0][0] == 99
    assert closed == [99]
    assert written == [b"\x1b]11;?\x07\x1b[c"]
