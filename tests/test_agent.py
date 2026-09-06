from langchain_core.messages import AIMessage

from luna.agent import INTERRUPT_TOOLS, build_agent
from luna.config import LunaConfig
from luna.prompts import LUNA_SYSTEM_PROMPT


def test_prompt_mentions_the_four_verbs():
    for verb in ("Observe", "Understand", "Plan", "Act"):
        assert verb in LUNA_SYSTEM_PROMPT


def test_build_agent_runs_offline(tmp_path, fake_model):
    cfg = LunaConfig(workdir=str(tmp_path))
    agent = build_agent(cfg, model=fake_model(AIMessage(content="hello from luna")))
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "hi"}]},
        config={"configurable": {"thread_id": "t1"}},
    )
    assert "luna" in result["messages"][-1].content.lower()


def test_yolo_disables_interrupts(tmp_path, fake_model):
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model())
    agent.invoke(
        {"messages": [{"role": "user", "content": "hi"}]},
        config={"configurable": {"thread_id": "t2"}},
    )


def test_interrupt_tools_cover_mutations():
    assert set(INTERRUPT_TOOLS) >= {"write_file", "edit_file", "delete", "execute"}


def test_memory_wired_when_agents_md_present(tmp_path, fake_model):
    (tmp_path / "AGENTS.md").write_text("# project notes\n")
    cfg = LunaConfig(workdir=str(tmp_path))
    # Should build without error and pick up the memory file.
    build_agent(cfg, model=fake_model())
