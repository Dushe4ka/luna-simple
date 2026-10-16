from luna.extensions.initgen import existing_action, init_prompt


def test_existing_action(tmp_path):
    assert existing_action(str(tmp_path)) == "create"
    (tmp_path / "AGENTS.md").write_text("# x\n")
    assert existing_action(str(tmp_path)) == "update"


def test_prompt_mentions_agents_md(tmp_path):
    p = init_prompt(str(tmp_path))
    assert "AGENTS.md" in p and ("build" in p.lower() or "test" in p.lower())


def test_init_subcommand_runs_agent(tmp_path, monkeypatch, capsys):
    from luna_core.providers.fake import FakeProvider

    from luna import cli
    from luna.core.agent import build_agent as real_build

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = FakeProvider([FakeProvider.text("wrote AGENTS.md")])

    async def build(config, **kw):
        return await real_build(config, provider=provider, **kw)

    monkeypatch.setattr(cli, "build_agent", build)
    rc = cli.main(["init"])
    assert rc == 0
    assert "AGENTS.md" in provider.requests[0].messages[-1].text  # the init prompt was sent
