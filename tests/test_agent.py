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


def test_build_agent_wires_extensions(tmp_path, fake_model, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    from luna.agent import describe_capabilities

    cfg = LunaConfig(workdir=str(tmp_path))
    agent = build_agent(cfg, model=fake_model())
    agent.invoke(
        {"messages": [{"role": "user", "content": "hi"}]},
        config={"configurable": {"thread_id": "ext"}},
    )
    caps = describe_capabilities(cfg)
    assert "researcher" in caps["subagents"]
    assert caps["tools"] >= 2  # manage_mcp + manage_skills


def test_subagent_deny_rule_is_enforced(tmp_path, fake_model):
    """An unsafe subagent's execute call is still blocked by a deny rule.

    Downgraded per the task brief to the direct-graph form: driving ``task``
    through ``FakeToolCallingModel`` is unreliable because the subagent shares
    the (already-drained) scripted model with the main agent. Here the subagent
    graph is built directly with the same ``guard`` middleware and a denied
    ``execute`` call must come back as the block message.
    """
    from deepagents import create_deep_agent
    from deepagents.backends import LocalShellBackend
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from luna.permissions import load_rules
    from luna.subagents import load_subagents
    from luna.toolguard import tool_guard

    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text('deny = ["execute:*"]\n')
    (tmp_path / ".luna" / "subagents.toml").write_text(
        '[subagent.impl]\ndescription = "impl"\nprompt = "you implement"\n'
        'tools = ["execute", "read_file"]\nunsafe = true\n'
    )
    rules = load_rules(str(tmp_path))
    guard = tool_guard(rules, str(tmp_path), session_id="sess")

    # The subagent loads without raising because unsafe = true is set.
    subs = load_subagents(str(tmp_path), guard=guard, on_warn=lambda _l: None)
    impl = next(s for s in subs if s["name"] == "impl")
    assert impl["middleware"][0] is guard

    exec_call = {"name": "execute", "id": "e1", "args": {"command": "ls"}}
    model = fake_model(
        AIMessage(content="", tool_calls=[exec_call]),
        AIMessage(content="done"),
    )
    sub_graph = create_deep_agent(
        model=model,
        system_prompt="you implement",
        backend=LocalShellBackend(root_dir=str(tmp_path), virtual_mode=True, inherit_env=True),
        middleware=[guard],
        checkpointer=InMemorySaver(),
    )
    out = sub_graph.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        config={"configurable": {"thread_id": "sub"}},
    )
    blob = " ".join(getattr(m, "content", "") or "" for m in out["messages"])
    assert "blocked by a Luna permission rule" in blob


def test_subagent_reads_a_real_file(tmp_path, fake_model):
    """End-to-end: main agent -> ``task`` -> ``researcher`` -> ``read_file`` on disk.

    The subagent's ``FilesystemMiddleware`` must carry the main agent's real
    ``LocalShellBackend``. Before the backend fix, deepagents replaced it with an
    ephemeral ``StateBackend`` and this read returned "File ... not found".

    Uses the true shared-queue form (one ``FakeToolCallingModel`` drives both the
    main agent and the subagent), and observes the subagent's actual
    ``read_file`` ``ToolMessage`` via ``stream(subgraphs=True)`` rather than a
    scripted echo, so the assertion proves the real read happened.
    """
    from langchain_core.messages import ToolMessage

    from luna.config import LunaConfig

    (tmp_path / "hello.txt").write_text("the-magic-string-42\n")
    model = fake_model(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "task",
                    "id": "t1",
                    "args": {
                        "description": "read hello.txt and report it",
                        "subagent_type": "researcher",
                    },
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "id": "r1", "args": {"file_path": "/hello.txt"}}],
        ),
        AIMessage(content="reported"),
        AIMessage(content="final"),
    )
    agent = build_agent(LunaConfig(workdir=str(tmp_path)), model=model)
    reads: list[str] = []
    for _ns, state in agent.stream(
        {"messages": [{"role": "user", "content": "delegate"}]},
        config={"configurable": {"thread_id": "sub-real"}},
        subgraphs=True,
        stream_mode="values",
    ):
        for m in state.get("messages", []):
            if isinstance(m, ToolMessage) and m.name == "read_file":
                reads.append(m.content)
    assert reads, "subagent never ran read_file"
    assert any("the-magic-string-42" in c for c in reads)
    assert not any("not found" in c.lower() for c in reads)


def test_memory_wired_when_agents_md_present(tmp_path, fake_model):
    (tmp_path / "AGENTS.md").write_text("# project notes\n")
    cfg = LunaConfig(workdir=str(tmp_path))
    # Should build without error and pick up the memory file.
    build_agent(cfg, model=fake_model())


def test_memory_wired_when_luna_memory_tier_present(tmp_path, fake_model):
    d = tmp_path / ".luna" / "memory"
    d.mkdir(parents=True)
    (d / "project.md").write_text("# what this is\n")
    cfg = LunaConfig(workdir=str(tmp_path))
    # create_deep_agent must accept the nested ".luna/memory/project.md" path.
    agent = build_agent(cfg, model=fake_model(AIMessage(content="ok")))
    agent.invoke(
        {"messages": [{"role": "user", "content": "hi"}]},
        config={"configurable": {"thread_id": "mem-tier"}},
    )


def test_plan_mode_blocks_mutating_tools(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "write_file", "id": "1", "args": {"file_path": "/a.py", "content": "x"}}
            ],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls), plan_flag=lambda: True)
    out = agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        config={"configurable": {"thread_id": "t"}},
    )
    blob = " ".join(getattr(m, "content", "") or "" for m in out["messages"])
    assert "plan mode" in blob
    assert not (tmp_path / "a.py").exists()


def test_plan_flag_none_means_never_blocked(tmp_path, fake_model):
    from langchain_core.messages import AIMessage

    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "write_file", "id": "1", "args": {"file_path": "/a.py", "content": "x"}}
            ],
        ),
        AIMessage(content="done"),
    ]
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True)
    agent = build_agent(cfg, model=fake_model(*calls))  # plan_flag defaults None
    agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        config={"configurable": {"thread_id": "t"}},
    )
    assert (tmp_path / "a.py").exists()
