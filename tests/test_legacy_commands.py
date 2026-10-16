"""TEMPORARY: the shared ``/undo`` and ``/compact`` handlers on the server's LangGraph agents.

The REPL runs these commands on luna-core (tests/test_commands.py); the
server still reaches the old handlers through the registry until the
commands are ported, so their behaviour stays covered here.
"""

from dataclasses import dataclass, field

from luna.commands import run_line
from luna.config.config import LunaConfig
from luna.config.usage import SessionUsage
from luna.turn.context import PinnedFiles


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


def test_compact_replaces_history_in_place(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    from luna.core._legacy_agent import build_agent_legacy as build_agent

    agent = build_agent(
        LunaConfig(workdir=str(tmp_path)),
        model=fake_model(AIMessage(content="chatter"), AIMessage(content="SUMMARY: did X and Y")),
    )
    cfg = {"configurable": {"thread_id": "keep"}}
    agent.invoke({"messages": [{"role": "user", "content": "hello"}]}, config=cfg)
    run_line("/compact", LegacyEnv(workdir=str(tmp_path), agent=agent, thread_id="keep"))
    msgs = agent.get_state(cfg).values["messages"]
    assert len(msgs) == 1 and "did X and Y" in msgs[0].content and msgs[0].type == "human"


def test_compact_no_summary_is_graceful(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    from luna.core._legacy_agent import build_agent_legacy as build_agent

    agent = build_agent(LunaConfig(workdir=str(tmp_path)), model=fake_model(AIMessage(content="")))
    res = run_line("/compact", LegacyEnv(workdir=str(tmp_path), agent=agent))
    assert res.notices[0].level == "error"


def test_compact_empty_summary_trims_the_instruction_turn(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    from luna.core._legacy_agent import build_agent_legacy as build_agent

    agent = build_agent(
        LunaConfig(workdir=str(tmp_path)),
        model=fake_model(AIMessage(content=""), AIMessage(content="")),
    )
    cfg = {"configurable": {"thread_id": "keep"}}
    agent.invoke({"messages": [{"role": "user", "content": "hello"}]}, config=cfg)
    before = len(agent.get_state(cfg).values["messages"])
    res = run_line("/compact", LegacyEnv(workdir=str(tmp_path), agent=agent, thread_id="keep"))
    assert len(agent.get_state(cfg).values["messages"]) == before
    assert "no summary produced" in res.notices[0].text
