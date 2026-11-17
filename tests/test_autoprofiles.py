"""Auto-profiles: distill flow, files, versions, undo, disable, hour gate."""

import asyncio
import json
import tomllib

import pytest
from luna_core.assemblies import AssemblyLog
from luna_core.providers.fake import FakeProvider
from luna_core.subagents import load_subagents

from luna.config.config import config_dir
from luna.extensions.autoprofiles import EVERY, HOUR, AutoProfiles, deliver_notices


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def journal(tmp_path):
    j = AssemblyLog(tmp_path / "s.db")
    yield j
    j.close()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def auto(journal, clock, isolated_config_home):
    return AutoProfiles(journal, clock=clock)


def add_runs(journal, n, *, workdir, ok=lambda i: True, tools=("read_file",), mutating=()):
    return [
        journal.record(
            workdir=str(workdir),
            parent_session="p",
            child_session=f"c{i}",
            base_profile="general-purpose",
            dynamic=True,
            spec={
                "name": "general-purpose",
                "prompt": "Write a SQL migration",
                "tools": list(tools),
                "skills": [],
                "model": None,
                "context": "",
            },
            task="add column",
            ok=ok(i),
            cancelled=False,
            hit_max_steps=False,
            steps=2,
            cost=0.0,
            mutating=mutating,
        ).id
        for i in range(n)
    ]


def reply(ids, *, name="migration-writer", tools=("read_file",)):
    return FakeProvider.text(
        json.dumps(
            {
                "profiles": [
                    {
                        "name": name,
                        "description": "Writes migrations",
                        "prompt": "You write SQL migrations.",
                        "tools": list(tools),
                        "skills": [],
                        "model": None,
                        "context": "",
                        "max_steps": 20,
                        "runs": list(ids),
                    }
                ]
            }
        )
    )


def table(path, name):
    return tomllib.loads(path.read_text())["subagent"][name]


async def test_saves_to_project_file_and_notifies(auto, journal, tmp_path):
    proj = tmp_path / "proj"
    ids = add_runs(journal, 6, workdir=proj, ok=lambda i: i != 0)
    provider = FakeProvider([reply(ids)])
    report = await auto.distill(provider, "fast", allow_write=False)
    assert report.saved == ["Luna сохранила профиль `migration-writer` (5/6 успешно)"]
    saved = table(proj / ".luna" / "subagents.toml", "migration-writer")
    assert saved["auto"] is True and saved["version"] == 1
    assert "unsafe" not in saved
    assert journal.pending() == []
    assert deliver_notices(journal, lambda lvl, txt: None) == 1
    # the saved file loads as a normal profile
    spec = load_subagents([proj / ".luna" / "subagents.toml"])["migration-writer"]
    assert spec.auto and spec.max_steps == 20


async def test_two_workdirs_go_to_user_file(auto, journal, tmp_path, isolated_config_home):
    ids = add_runs(journal, 3, workdir=tmp_path / "a") + add_runs(
        journal, 3, workdir=tmp_path / "b"
    )
    await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    assert table(config_dir() / "subagents.toml", "migration-writer")
    assert not (tmp_path / "a" / ".luna" / "subagents.toml").exists()


async def test_below_threshold_saves_nothing(auto, journal, tmp_path):
    ids = add_runs(journal, 4, workdir=tmp_path)
    report = await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    assert report.saved == [] and report.rejected[0][0] == "migration-writer"
    assert len(journal.pending()) == 4


async def test_manual_profile_is_never_touched(auto, journal, tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    f.parent.mkdir()
    original = '# mine\n[subagent.migration-writer]\nprompt = "hand"\n'
    f.write_text(original)
    ids = add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    assert report.saved == [] and "hand-written" in report.rejected[0][1]
    assert f.read_text() == original


async def test_builtin_name_is_protected(auto, journal, tmp_path):
    ids = add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(
        FakeProvider([reply(ids, name="researcher")]), "fast", allow_write=False
    )
    assert report.saved == []


async def test_auto_profile_update_bumps_version_keeps_comments(auto, journal, clock, tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    f.parent.mkdir()
    f.write_text('# keep me\n[subagent.other]\nprompt = "x"  # and me\n')
    await auto.distill(
        FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]), "fast", allow_write=False
    )
    clock.now += HOUR
    await auto.distill(
        FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]), "fast", allow_write=False
    )
    text = f.read_text()
    assert "# keep me" in text and "# and me" in text
    assert table(f, "migration-writer")["version"] == 2
    assert [v.action for v in journal.versions("migration-writer")] == ["update", "save"]


async def test_write_ceiling_and_unsafe(auto, journal, tmp_path):
    ids = add_runs(journal, 5, workdir=tmp_path, tools=("read_file", "write_file"))
    denied = await auto.distill(
        FakeProvider([reply(ids, tools=("write_file",))]), "fast", allow_write=False
    )
    assert denied.saved == [] and "allow_write" in denied.rejected[0][1]
    auto._clock.now += HOUR
    allowed = await auto.distill(
        FakeProvider([reply(ids, tools=("write_file",))]), "fast", allow_write=True
    )
    assert allowed.saved
    assert table(tmp_path / ".luna" / "subagents.toml", "migration-writer")["unsafe"] is True


async def test_hour_gate_and_single_flight(auto, journal, clock, tmp_path):
    ids = add_runs(journal, 6, workdir=tmp_path)
    provider = FakeProvider([reply(ids), reply(ids)])
    first, second = await asyncio.gather(
        auto.distill(provider, "fast", allow_write=False),
        auto.distill(provider, "fast", allow_write=False),
    )
    assert len(provider.requests) == 1
    assert "recently" in (first.skipped or second.skipped)
    clock.now += HOUR - 60
    assert "recently" in (await auto.distill(provider, "fast", allow_write=False)).skipped
    assert len(provider.requests) == 1


async def test_failed_request_still_counts_for_the_gate(auto, journal, clock, tmp_path):
    from luna_core.errors import ProviderError

    class Broken:
        name = "broken"

        async def stream(self, request):
            raise ProviderError("boom")
            yield  # pragma: no cover

    add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(Broken(), "fast", allow_write=False)
    assert "analysis failed" in report.skipped
    assert "recently" in (await auto.distill(Broken(), "fast", allow_write=False)).skipped


async def test_garbage_reply_saves_nothing(auto, journal, tmp_path):
    add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(
        FakeProvider([FakeProvider.text("sorry, no JSON")]), "fast", allow_write=False
    )
    assert report.saved == [] and report.rejected == []


async def test_broken_toml_is_not_overwritten(auto, journal, tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    f.parent.mkdir()
    f.write_text("[subagent.x\n")
    ids = add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    assert report.saved == [] and "cannot read" in report.rejected[0][1]
    assert f.read_text() == "[subagent.x\n"


def test_due_by_count_and_weekly(auto, journal, clock, tmp_path):
    assert not auto.due_weekly()
    add_runs(journal, EVERY - 1, workdir=tmp_path)
    assert not auto.due_by_count() and auto.due_weekly()
    add_runs(journal, 1, workdir=tmp_path)
    assert auto.due_by_count()


async def test_auto_disable(auto, journal, tmp_path):
    ids = add_runs(journal, 5, workdir=tmp_path)
    await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    for i in range(5):
        journal.record(
            workdir=str(tmp_path),
            parent_session="p",
            child_session=f"x{i}",
            base_profile="migration-writer",
            dynamic=False,
            spec={"tools": []},
            task="t",
            ok=i == 0,
            cancelled=False,
            hit_max_steps=False,
            steps=1,
            cost=0.0,
        )
    text = auto.check_disable("migration-writer")
    assert text and "1" in text
    f = tmp_path / ".luna" / "subagents.toml"
    assert table(f, "migration-writer")["disabled"] is True
    assert "migration-writer" not in load_subagents([f])
    assert auto.check_disable("migration-writer") is None  # already off


async def test_undo_restores_previous_version_then_removes(auto, journal, clock, tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    await auto.distill(
        FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]), "fast", allow_write=False
    )
    clock.now += HOUR
    await auto.distill(
        FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]), "fast", allow_write=False
    )
    assert "version 1" in auto.undo("migration-writer")
    assert table(f, "migration-writer")["version"] == 1
    assert "removed" in auto.undo("migration-writer")
    assert "migration-writer" not in tomllib.loads(f.read_text()).get("subagent", {})
    assert "nothing to undo" in auto.undo("migration-writer")


async def test_undo_refuses_after_hand_edit(auto, journal, tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    await auto.distill(
        FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]), "fast", allow_write=False
    )
    f.write_text(f.read_text().replace("You write SQL migrations.", "Edited by me."))
    before = f.read_text()
    assert "by hand" in auto.undo("migration-writer")
    assert f.read_text() == before


def test_disable_enable_manual_and_builtin(auto, tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    f.parent.mkdir()
    f.write_text('[subagent.mine]\nprompt = "p"\n')
    auto.set_disabled("mine", True, workdir=str(tmp_path))
    assert table(f, "mine")["disabled"] is True
    auto.set_disabled("mine", False, workdir=str(tmp_path))
    assert "disabled" not in table(f, "mine")
    assert "built-in" in auto.set_disabled("researcher", True, workdir=str(tmp_path))


async def test_history_lines(auto, journal, tmp_path):
    await auto.distill(
        FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]), "fast", allow_write=False
    )
    (line,) = auto.history()
    assert "save" in line and "migration-writer v1" in line


async def test_made_up_model_is_not_saved(auto, journal, tmp_path):
    ids = add_runs(journal, 5, workdir=tmp_path)
    payload = {
        "profiles": [
            {
                "name": "migration-writer",
                "description": "d",
                "prompt": "p",
                "tools": ["read_file"],
                "skills": [],
                "model": "gpt-made-up",
                "context": "",
                "max_steps": 20,
                "runs": ids,
            }
        ]
    }
    report = await auto.distill(
        FakeProvider([FakeProvider.text(json.dumps(payload))]), "fast", allow_write=False
    )
    assert report.saved
    assert "model" not in table(tmp_path / ".luna" / "subagents.toml", "migration-writer")


async def test_known_model_is_kept(auto, journal, tmp_path):
    ids = [
        journal.record(
            workdir=str(tmp_path),
            parent_session="p",
            child_session=f"c{i}",
            base_profile="general-purpose",
            dynamic=True,
            spec={"prompt": "x", "tools": ["read_file"], "skills": [], "model": "real-m"},
            task="t",
            ok=True,
            cancelled=False,
            hit_max_steps=False,
            steps=1,
            cost=0.0,
        ).id
        for i in range(5)
    ]
    payload = {
        "profiles": [
            {
                "name": "migration-writer",
                "description": "d",
                "prompt": "p",
                "tools": ["read_file"],
                "skills": [],
                "model": "real-m",
                "context": "",
                "max_steps": 20,
                "runs": ids,
            }
        ]
    }
    await auto.distill(
        FakeProvider([FakeProvider.text(json.dumps(payload))]), "fast", allow_write=False
    )
    assert table(tmp_path / ".luna" / "subagents.toml", "migration-writer")["model"] == "real-m"


async def test_duplicate_name_in_one_analysis(auto, journal, tmp_path):
    ids = add_runs(journal, 6, workdir=tmp_path)
    item = {
        "name": "migration-writer",
        "description": "d",
        "prompt": "p",
        "tools": ["read_file"],
        "skills": [],
        "model": None,
        "context": "",
        "max_steps": 20,
        "runs": ids,
    }
    payload = {"profiles": [item, dict(item)]}
    report = await auto.distill(
        FakeProvider([FakeProvider.text(json.dumps(payload))]), "fast", allow_write=False
    )
    assert len(report.saved) == 1
    assert report.rejected == [("migration-writer", "duplicate name in this analysis")]
    assert len(journal.versions("migration-writer")) == 1


async def test_run_counts_for_one_profile_only(auto, journal, tmp_path):
    ids = add_runs(journal, 9, workdir=tmp_path)
    base = {
        "description": "d",
        "prompt": "p",
        "tools": ["read_file"],
        "skills": [],
        "model": None,
        "context": "",
        "max_steps": 20,
    }
    payload = {
        "profiles": [
            {**base, "name": "first-one", "runs": ids[:6]},
            {**base, "name": "second-one", "runs": ids[3:]},
        ]
    }
    report = await auto.distill(
        FakeProvider([FakeProvider.text(json.dumps(payload))]), "fast", allow_write=False
    )
    assert len(report.saved) == 1 and "first-one" in report.saved[0]
    assert report.rejected == [("second-one", "only 3 runs (needs 5)")]
    names = {r.id: r.profile_name for r in (journal.get(i) for i in ids)}
    assert [names[i] for i in ids] == ["first-one"] * 6 + [None] * 3


async def test_manual_user_profile_blocks_a_project_auto_profile(
    auto, journal, tmp_path, isolated_config_home
):
    user = config_dir() / "subagents.toml"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text('[subagent.migration-writer]\nprompt = "hand"\n')
    ids = add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    assert report.saved == [] and "hand-written" in report.rejected[0][1]
    assert not (tmp_path / ".luna" / "subagents.toml").exists()


@pytest.mark.parametrize("value", ["subagent = 1\n", "subagent = { x = { prompt = 'p' } }\n"])
async def test_subagent_key_that_is_not_a_table_is_rejected(auto, journal, tmp_path, value):
    f = tmp_path / ".luna" / "subagents.toml"
    f.parent.mkdir()
    f.write_text(value)
    ids = add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    assert report.saved == []
    assert "'subagent' must be a table" in report.rejected[0][1]
    assert f.read_text() == value


def static_runs(journal, n, *, workdir, ok, dynamic=False, name="migration-writer"):
    for i in range(n):
        journal.record(
            workdir=str(workdir),
            parent_session="p",
            child_session=f"s{i}",
            base_profile=name,
            dynamic=dynamic,
            spec={"tools": []},
            task="t",
            ok=ok(i),
            cancelled=False,
            hit_max_steps=False,
            steps=1,
            cost=0.0,
        )


async def test_enable_after_auto_disable_sticks(auto, journal, tmp_path):
    ids = add_runs(journal, 5, workdir=tmp_path)
    await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    static_runs(journal, 5, workdir=tmp_path, ok=lambda i: False)
    assert auto.check_disable("migration-writer")
    auto.set_disabled("migration-writer", False, workdir=str(tmp_path))
    static_runs(journal, 1, workdir=tmp_path, ok=lambda i: True)
    assert auto.check_disable("migration-writer") is None
    f = tmp_path / ".luna" / "subagents.toml"
    assert "disabled" not in table(f, "migration-writer")


async def test_dynamic_and_other_project_runs_do_not_disable(auto, journal, tmp_path):
    ids = add_runs(journal, 5, workdir=tmp_path)
    await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    static_runs(journal, 5, workdir=tmp_path, ok=lambda i: False, dynamic=True)
    static_runs(journal, 5, workdir=tmp_path / "elsewhere", ok=lambda i: False)
    assert auto.check_disable("migration-writer") is None


async def test_update_starts_a_fresh_window(auto, journal, clock, tmp_path):
    ids = add_runs(journal, 5, workdir=tmp_path)
    await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    static_runs(journal, 4, workdir=tmp_path, ok=lambda i: False)
    clock.now += HOUR
    ids = add_runs(journal, 5, workdir=tmp_path)
    report = await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    assert report.saved  # v2
    static_runs(journal, 1, workdir=tmp_path, ok=lambda i: False)
    assert auto.check_disable("migration-writer") is None
