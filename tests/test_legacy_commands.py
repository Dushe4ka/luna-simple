"""TEMPORARY: the shared registry's file-journal ``/undo`` and ``/compact`` handlers.

The REPL and the server run these commands on luna-core
(``luna.repl.commands.CORE_COMMANDS``); the registry handlers stay until
Task 31 ports the commands.
"""

from dataclasses import dataclass, field

from luna_core.context import PinnedFiles

from luna.commands import run_line
from luna.config.config import LunaConfig
from luna.config.usage import SessionUsage


@dataclass
class LegacyEnv:
    workdir: str = "."
    thread_id: str = "t"
    session_id: str = "s1"
    index: object = None
    user_commands: dict = field(default_factory=dict)
    pinned: PinnedFiles = field(default_factory=PinnedFiles)
    usage: SessionUsage = field(default_factory=SessionUsage)
    agent: object = None
    can_rebuild: bool = False

    @property
    def config(self):
        return LunaConfig(workdir=self.workdir)


def _snapshot_edit(tmp_path):
    from luna.turn.undo import snapshot

    f = tmp_path / "a.py"
    f.write_text("old\n")
    snapshot(str(tmp_path), "s1", "edit_file", "a.py")
    f.write_text("new\n")
    return f


def test_undo_asks_first_and_keeps_the_entry(tmp_path):
    from luna.turn.undo import journal_dir

    f = _snapshot_edit(tmp_path)
    res = run_line("/undo", LegacyEnv(workdir=str(tmp_path)))
    assert res.confirm is not None and res.confirm.resubmit == "/undo --yes"
    assert f.read_text() == "new\n"  # file untouched
    assert list(journal_dir(str(tmp_path), "s1").glob("[0-9]*.json"))  # entry kept


def test_undo_confirmed_reverts(tmp_path):
    f = _snapshot_edit(tmp_path)
    run_line("/undo --yes", LegacyEnv(workdir=str(tmp_path)))
    assert f.read_text() == "old\n"


def test_compact_failure_does_not_raise(tmp_path):
    class _BoomAgent:
        def get_state(self, config):
            raise RuntimeError("boom")

    res = run_line("/compact", LegacyEnv(workdir=str(tmp_path), agent=_BoomAgent()))
    assert "/compact failed" in res.notices[0].text
