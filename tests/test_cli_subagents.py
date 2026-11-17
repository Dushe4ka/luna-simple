"""luna subagents: history / undo / disable / enable / distill."""

from luna.cli import main
from luna.extensions import autoprofiles
from luna.extensions.autoprofiles import open_journal


def test_history_empty(capsys):
    assert main(["subagents"]) == 0
    assert "no profile changes yet" in capsys.readouterr().out


def test_disable_enable_and_unknown(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / ".luna" / "subagents.toml"
    f.parent.mkdir()
    f.write_text('[subagent.mine]\nprompt = "p"\n')
    assert main(["subagents", "disable", "mine"]) == 0
    assert "disabled = true" in f.read_text()
    assert main(["subagents", "enable", "mine"]) == 0
    assert main(["subagents", "disable", "researcher"]) == 1
    assert "built-in" in capsys.readouterr().out


def test_undo_nothing(capsys):
    assert main(["subagents", "undo", "ghost"]) == 1
    assert "nothing to undo" in capsys.readouterr().out


def test_distill_uses_fast_role_and_prints_report(monkeypatch, capsys):
    from luna_core.providers.fake import FakeProvider

    provider = FakeProvider([FakeProvider.text('{"profiles": []}')])
    monkeypatch.setattr(autoprofiles, "fast_target", lambda config: (provider, "fast-m"))
    open_journal().record(
        workdir="/w",
        parent_session="p",
        child_session="c",
        base_profile="gp",
        dynamic=True,
        spec={},
        task="t",
        ok=True,
        cancelled=False,
        hit_max_steps=False,
        steps=1,
        cost=0.0,
    )
    assert main(["subagents", "distill"]) == 0
    assert "no repeating assemblies found" in capsys.readouterr().out
    assert provider.requests[0].model == "fast-m"
    assert main(["subagents", "distill"]) == 0  # within the hour
    assert "recently" in capsys.readouterr().out
    assert len(provider.requests) == 1
