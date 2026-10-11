import pytest
from luna_core.events import NeedsApproval, Notice, TurnDone
from luna_core.loop import Agent
from luna_core.messages import Image, Message, ToolCall
from luna_core.policy import Decision, Policy
from luna_core.providers.fake import FakeProvider
from luna_core.roles import ModelRoles
from luna_core.session import EventLog, Session
from luna_core.workspace import Workspace

OVERRIDES = {
    "text-only": {"window": 100000, "vision": False},
    "seeing": {"window": 100000, "vision": True},
}


@pytest.fixture
def session(tmp_path):
    log = EventLog(tmp_path / "s.db")
    yield Session.create(log, str(tmp_path), undo=False)
    log.close()


def test_role_resolution():
    main, other = FakeProvider([]), FakeProvider([])
    roles = ModelRoles(fast="small", vision=(other, "seeing"), plan="thinker")
    assert roles.get("fast", main, "big") == (main, "small")
    assert roles.get("summarize", main, "big") == (main, "big")
    assert roles.for_turn(main, "text-only", images=True, plan=False, overrides=OVERRIDES) == (
        other,
        "seeing",
        "vision",
    )
    assert roles.for_turn(main, "seeing", images=True, plan=False, overrides=OVERRIDES) == (
        main,
        "seeing",
        None,
    )
    assert roles.for_turn(main, "big", images=False, plan=True) == (main, "thinker", "plan")


async def test_image_turn_switches_to_the_vision_model(tmp_path, session):
    vision = FakeProvider([FakeProvider.text("a cat")])
    agent = Agent(
        FakeProvider([]),
        "text-only",
        tools=[],
        workspace=Workspace(tmp_path),
        roles=ModelRoles(vision=(vision, "seeing")),
        model_overrides=OVERRIDES,
    )
    events = [
        e async for e in agent.run(session, "what is it?", images=[Image("image/png", "AAA")])
    ]
    assert any(isinstance(e, Notice) and "seeing" in e.text for e in events) and isinstance(
        events[-1], TurnDone
    )
    sent = vision.requests[0]
    assert sent.model == "seeing" and isinstance(sent.messages[0].content[1], Image)


async def test_plan_mode_uses_the_plan_model(tmp_path, session):
    main = FakeProvider([FakeProvider.text("plan: step 1")])
    agent = Agent(
        main,
        "big",
        tools=[],
        workspace=Workspace(tmp_path),
        roles=ModelRoles(plan="thinker"),
        policy=Policy(workspace=Workspace(tmp_path), plan_mode=True),
    )
    [e async for e in agent.run(session, "plan it")]
    assert main.requests[0].model == "thinker"


async def test_images_without_vision_warn(tmp_path, session):
    agent = Agent(
        FakeProvider([FakeProvider.text("?")]),
        "text-only",
        tools=[],
        workspace=Workspace(tmp_path),
        model_overrides=OVERRIDES,
    )
    events = [e async for e in agent.run(session, "look", images=[Image("image/png", "AAA")])]
    assert any(isinstance(e, Notice) and e.level == "warn" and "images" in e.text for e in events)


async def test_side_question_is_not_logged(tmp_path, session):
    main = FakeProvider([FakeProvider.text("main answer"), FakeProvider.text("side answer")])
    agent = Agent(main, "big", workspace=Workspace(tmp_path))
    [e async for e in agent.run(session, "main question")]
    before = len(session.records())
    answer = await agent.side_question(session, "btw, what is 2+2?")
    assert answer.text == "side answer" and len(session.records()) == before
    sent = main.requests[1]
    assert (
        sent.tools == []
        and sent.messages[-1].text == "btw, what is 2+2?"
        and sent.messages[-2].text == "main answer"
    )


async def test_steer_while_waiting_for_approval_arrives_after_the_tool_batch(tmp_path, session):
    main = FakeProvider(
        [FakeProvider.call("execute", {"command": "echo hi"}, "c1"), FakeProvider.text("ok")]
    )
    agent = Agent(main, "big", workspace=Workspace(tmp_path))
    async for event in agent.run(session, "go"):
        if isinstance(event, NeedsApproval):
            session.queue("use -n instead")
            session.decide("c1", Decision.approve())
    second = main.requests[1].messages
    assert second[-2].role == "tool" and second[-1] == Message.user("use -n instead")
    assert isinstance(second[-3].content[0], ToolCall)
