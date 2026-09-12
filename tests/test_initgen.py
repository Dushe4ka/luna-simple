from luna.extensions.initgen import existing_action, init_prompt


def test_existing_action(tmp_path):
    assert existing_action(str(tmp_path)) == "create"
    (tmp_path / "AGENTS.md").write_text("# x\n")
    assert existing_action(str(tmp_path)) == "update"


def test_prompt_mentions_agents_md(tmp_path):
    p = init_prompt(str(tmp_path))
    assert "AGENTS.md" in p and ("build" in p.lower() or "test" in p.lower())


def test_init_subcommand_runs_agent(tmp_path, fake_model, monkeypatch, capsys):
    from langchain_core.messages import AIMessage

    from luna import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(
        cli,
        "build_agent",
        lambda *a, **k: __import__("luna.core.agent", fromlist=["build_agent"]).build_agent(
            *a,
            model=fake_model(AIMessage(content="wrote AGENTS.md")),
            **{kk: vv for kk, vv in k.items() if kk != "model"},
        ),
    )
    rc = cli.main(["init"])
    assert rc == 0
