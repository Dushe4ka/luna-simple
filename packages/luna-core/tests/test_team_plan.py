"""The team plan: parsing, checks and the read-only planner."""

import copy

import pytest
from luna_core.errors import LunaCoreError
from luna_core.messages import Text, ToolResult
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session
from luna_core.team.plan import PlanError, Planner, TeamPlan, parse_plan
from luna_core.tools import builtin_tools
from luna_core.workspace import Workspace

GOOD = {
    "goal": "add a CLI flag",
    "criteria": ["tests pass"],
    "tasks": [
        {"id": "T1", "title": "read code", "kind": "research"},
        {"id": "T2", "title": "implement", "kind": "code", "deps": ["T1"], "member": "dev"},
    ],
    "members": [
        {
            "name": "dev",
            "prompt": "You write Python.",
            "tools": ["read_file", "edit_file"],
            "model": "fast",
        },
        {"name": "scout", "profile": "researcher"},
    ],
    "allow": ["execute:uv run pytest*"],
    "estimate": {"minutes": 20, "requests": 40},
}


def test_good_plan_parses_and_roundtrips():
    plan = parse_plan(GOOD, max_members=8, profiles={"researcher"})
    assert [t.id for t in plan.tasks] == ["T1", "T2"] and plan.tasks[1].deps == ("T1",)
    assert plan.members[0].assembly() == {
        "prompt": "You write Python.",
        "tools": ["read_file", "edit_file"],
        "model": "fast",
    }
    assert plan.members[1].assembly() == {}
    assert TeamPlan.from_dict(plan.to_dict()) == plan
    assert plan.estimate == {"minutes": 20, "requests": 40}


def test_ordered_tasks_put_dependencies_first():
    data = copy.deepcopy(GOOD)
    data["tasks"].reverse()
    plan = parse_plan(data, max_members=8, profiles={"researcher"})
    assert [t.id for t in plan.ordered_tasks()] == ["T1", "T2"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["tasks"][1].update(deps=["T9"]), "unknown dependencies"),
        (lambda d: d["tasks"][0].update(deps=["T2"]), "cycle"),
        (lambda d: d["tasks"][0].update(kind="poem"), "kind"),
        (lambda d: d["tasks"][1].update(member="ghost"), "unknown member"),
        (lambda d: d["members"].append({"name": "dev", "prompt": "x"}), "duplicate"),
        (lambda d: d["members"][1].update(profile="nope"), "unknown profile"),
        (lambda d: d["members"].append({"name": "lead", "prompt": "x"}), "lead"),
        (lambda d: d.update(allow=["execute:*"]), "plain program"),
        (lambda d: d.update(allow=["execute:bash *"]), "can run any command"),
        (lambda d: d.update(allow=["pytest"]), "<tool>:<pattern>"),
        (lambda d: d.update(criteria=[]), "criteria"),
        (lambda d: d["members"][1].pop("profile"), "profile or a prompt"),
        (lambda d: d.update(tasks=[]), "at least one task"),
    ],
)
def test_bad_plans_are_refused_with_a_reason(change, message):
    data = copy.deepcopy(GOOD)
    change(data)
    with pytest.raises(PlanError, match=message):
        parse_plan(data, max_members=8, profiles={"researcher"})


def test_member_limit():
    with pytest.raises(PlanError, match="at most 1 members"):
        parse_plan(GOOD, max_members=1, profiles={"researcher"})


def results(session: Session) -> dict[str, tuple[bool, str]]:
    found = {}
    for message in session.messages():
        for block in message.content:
            if isinstance(block, ToolResult):
                text = "".join(b.text for b in block.content if isinstance(b, Text))
                found[block.call_id] = (block.is_error, text)
    return found


@pytest.fixture
def session(tmp_path):
    log = EventLog(tmp_path / "db" / "s.db")
    yield Session.create(log, str(tmp_path), undo=False)
    log.close()


def planner(tmp_path, provider, **kwargs) -> Planner:
    return Planner(
        provider,
        "strong-model",
        workspace=Workspace(tmp_path),
        tools=builtin_tools(),
        max_members=8,
        profiles={"researcher"},
        max_steps=10,
        **kwargs,
    )


async def test_planner_explores_read_only_and_returns_the_submitted_plan(tmp_path, session):
    (tmp_path / "app.py").write_text("print('hi')\n")
    provider = FakeProvider(
        [
            FakeProvider.call("read_file", {"file_path": "app.py"}, "c1"),
            FakeProvider.call("execute", {"command": "rm -rf ."}, "c2"),
            FakeProvider.call("submit_plan", {**GOOD, "tasks": []}, "c3"),
            FakeProvider.call("submit_plan", GOOD, "c4"),
            FakeProvider.text("plan submitted"),
        ]
    )
    plan = await planner(tmp_path, provider).plan(session, "add a CLI flag")
    assert plan.goal == "add a CLI flag"
    found = results(session)
    assert found["c1"][0] is False and "print" in found["c1"][1]
    assert found["c2"][0] is True
    assert found["c3"][0] is True and "plan rejected" in found["c3"][1]
    assert found["c4"] == (False, "plan recorded; end your turn now")
    assert (tmp_path / "app.py").exists()
    assert "Team lead" in provider.requests[0].system
    assert {t.name for t in provider.requests[0].tools} == {
        "ls",
        "read_file",
        "glob",
        "grep",
        "submit_plan",
    }


async def test_edit_feedback_reaches_the_lead_with_the_previous_plan(tmp_path, session):
    provider = FakeProvider([FakeProvider.call("submit_plan", GOOD, "c1"), FakeProvider.text("ok")])
    previous = parse_plan(GOOD, max_members=8, profiles={"researcher"})
    await planner(tmp_path, provider).plan(
        session, "add a CLI flag", feedback="use two developers", previous=previous
    )
    first = provider.requests[0].messages[-1].text
    assert "use two developers" in first and '"goal": "add a CLI flag"' in first


async def test_member_check_rejections_go_back_to_the_lead(tmp_path, session):
    def check(member):
        if member.tools and "nuke" in member.tools:
            raise LunaCoreError("unknown tools ['nuke']")

    bad = copy.deepcopy(GOOD)
    bad["members"][0]["tools"] = ["nuke"]
    provider = FakeProvider(
        [
            FakeProvider.call("submit_plan", bad, "c1"),
            FakeProvider.call("submit_plan", GOOD, "c2"),
            FakeProvider.text("ok"),
        ]
    )
    plan = await planner(tmp_path, provider, check_member=check).plan(session, "x")
    assert plan.members[0].tools == ("read_file", "edit_file")
    assert "unknown tools" in results(session)["c1"][1]


async def test_no_plan_is_an_error(tmp_path, session):
    provider = FakeProvider([FakeProvider.text("I refuse")])
    with pytest.raises(PlanError, match="without submitting"):
        await planner(tmp_path, provider).plan(session, "x")
