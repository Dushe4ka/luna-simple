"""Keeping the machine awake during a team run."""

import os

from luna_core.team import inhibit
from luna_core.team.inhibit import keep_awake


class FakeProc:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.pid = 4242
        self.waited = False

    def wait(self, timeout=None):
        self.waited = True
        return 0


def spawner(started):
    def popen(argv, **kwargs):
        proc = FakeProc(argv, **kwargs)
        started.append(proc)
        return proc

    return popen


def test_macos_uses_caffeinate_tied_to_our_pid(monkeypatch):
    killed, started = [], []
    monkeypatch.setattr(inhibit, "kill_tree", killed.append)
    with keep_awake(
        platform="darwin", which=lambda name: f"/usr/bin/{name}", popen=spawner(started)
    ) as held:
        assert held is True
        assert started[0].argv == ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]
    assert killed == [4242] and started[0].waited


def test_linux_uses_systemd_inhibit_when_present(monkeypatch):
    monkeypatch.setattr(inhibit, "kill_tree", lambda pid: None)
    started = []
    with keep_awake(platform="linux", which=lambda name: f"/bin/{name}", popen=spawner(started)):
        assert started[0].argv[0] == "/bin/systemd-inhibit"
        assert "--what=idle:sleep" in started[0].argv


def test_nothing_available_or_disabled_yields_false():
    with keep_awake(platform="linux", which=lambda name: None) as held:
        assert held is False
    with keep_awake(False, platform="darwin", which=lambda name: "/x") as held:
        assert held is False


def test_a_failing_spawn_is_not_fatal():
    def broken(argv, **kwargs):
        raise OSError("no such file")

    with keep_awake(platform="darwin", which=lambda name: "/x", popen=broken) as held:
        assert held is False


def test_windows_sets_and_clears_the_execution_state():
    calls = []

    class Kernel32:
        def SetThreadExecutionState(self, flags):  # noqa: N802 - Win32 name
            calls.append(flags)
            return 1

    with keep_awake(platform="win32", kernel32=Kernel32()) as held:
        assert held is True
    assert calls == [
        inhibit.ES_CONTINUOUS | inhibit.ES_SYSTEM_REQUIRED,
        inhibit.ES_CONTINUOUS,
    ]
