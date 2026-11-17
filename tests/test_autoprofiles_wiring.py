"""Wiring: runs reach the journal, triggers fire, notices are delivered."""

import asyncio

from luna_core.assemblies import AssemblyLog
from luna_core.providers.fake import FakeProvider

from luna.extensions import autoprofiles
from luna.extensions.autoprofiles import AutoProfiles, make_on_recorded


async def test_build_agent_journals_subagent_runs(tmp_path, monkeypatch):
    from luna.config.config import LunaConfig
    from luna.core.agent import build_agent

    provider = FakeProvider(
        [
            FakeProvider.call("task", {"subagent_type": "researcher", "description": "look"}),
            FakeProvider.text("child done"),
            FakeProvider.text("parent done"),
        ]
    )
    bundle = await build_agent(LunaConfig(workdir=str(tmp_path)), provider=provider)
    runner = bundle.agent.tools["task"].runner
    assert runner.journal is autoprofiles.open_journal()
    assert runner.on_recorded is not None


async def test_on_recorded_starts_one_background_distill(tmp_path, monkeypatch):
    journal = AssemblyLog(tmp_path / "s.db")
    auto = AutoProfiles(journal)
    calls = []

    async def fake_distill(provider, model, *, allow_write):
        calls.append((model, allow_write))
        await asyncio.sleep(0)

    monkeypatch.setattr(auto, "distill", fake_distill)
    monkeypatch.setattr(autoprofiles, "EVERY", 2)
    hook = make_on_recorded(auto, fast=(object(), "fast-model"), allow_write=False)
    for i in range(3):
        rec = journal.record(
            workdir="/w",
            parent_session="p",
            child_session=f"c{i}",
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
        hook(rec, None)
    await asyncio.sleep(0.01)
    assert calls == [("fast-model", False)]  # one live task at a time
    journal.close()


async def test_on_recorded_checks_auto_disable(tmp_path, monkeypatch):
    journal = AssemblyLog(tmp_path / "s.db")
    auto = AutoProfiles(journal)
    seen = []
    monkeypatch.setattr(auto, "check_disable", lambda name: seen.append(name))
    journal.add_version(
        name="mw",
        path=str(tmp_path / "x.toml"),
        action="save",
        version=1,
        spec={},
        prev=None,
    )
    hook = make_on_recorded(auto, fast=(object(), "m"), allow_write=False)
    rec = journal.record(
        workdir="/w",
        parent_session="p",
        child_session="c",
        base_profile="mw",
        dynamic=False,
        spec={},
        task="t",
        ok=False,
        cancelled=False,
        hit_max_steps=False,
        steps=1,
        cost=0.0,
    )
    hook(rec, None)
    other = journal.record(
        workdir="/w",
        parent_session="p",
        child_session="d",
        base_profile="researcher",
        dynamic=False,
        spec={},
        task="t",
        ok=False,
        cancelled=False,
        hit_max_steps=False,
        steps=1,
        cost=0.0,
    )
    hook(other, None)
    assert seen == ["mw"]
    journal.close()


async def test_runtime_after_turn_delivers_notices(monkeypatch):
    from luna_core.events import Notice

    from luna.server import runtime as runtime_mod

    journal = autoprofiles.open_journal()
    journal.push_notice("info", "Luna сохранила профиль `x` (5/5 успешно)")
    notified = []

    class FakeSession:
        def notify(self, event):
            notified.append(event)

    rt = runtime_mod.SessionRuntime.__new__(runtime_mod.SessionRuntime)
    rt.session = FakeSession()
    rt.reload_requested = rt.reload_after_turn = False
    monkeypatch.setattr(rt, "save", lambda: None, raising=False)
    await rt.after_turn()
    assert [e.text for e in notified if isinstance(e, Notice)] == [
        "Luna сохранила профиль `x` (5/5 успешно)"
    ]


async def test_weekly_distill_skips_without_records(monkeypatch):
    from luna.config.config import LunaConfig

    called = []
    monkeypatch.setattr(AutoProfiles, "distill", lambda *a, **k: called.append(1))
    await autoprofiles.weekly_distill(LunaConfig())
    assert called == []
