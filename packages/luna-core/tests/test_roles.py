import pytest
from luna_core.compaction import Compactor
from luna_core.events import Compacted, NeedsApproval, Notice, TurnDone
from luna_core.loop import Agent
from luna_core.messages import Image, Message, Text, ToolCall, message_to_dict
from luna_core.policy import Decision, Policy
from luna_core.providers.fake import FakeProvider
from luna_core.roles import ModelRoles
from luna_core.session import EventLog, Session
from luna_core.usage import Usage
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


async def test_compaction_uses_the_turns_resolved_model_window(tmp_path, session):
    for i in range(6):
        session.append("turn_started", {"turn_id": f"t{i}"})
        session.append(
            "user", {"message": message_to_dict(Message.user(f"question {i} " + "x" * 400))}
        )
        session.append(
            "assistant",
            {
                "message": message_to_dict(Message.assistant([Text(f"answer {i} " + "y" * 400)])),
                "usage": Usage().to_dict(),
            },
        )
    main = FakeProvider([FakeProvider.text("ok")])
    summarizer = FakeProvider([FakeProvider.text("SUMMARY")])
    overrides = {"big": {"window": 100000}, "thinker": {"window": 1000}}
    compactor = Compactor(threshold=0.8, keep_turns=1, provider=summarizer)
    agent = Agent(
        main,
        "big",
        tools=[],
        workspace=Workspace(tmp_path),
        roles=ModelRoles(plan="thinker"),
        policy=Policy(workspace=Workspace(tmp_path), plan_mode=True),
        model_overrides=overrides,
        compactor=compactor,
    )
    events = [e async for e in agent.run(session, "next")]
    assert any(isinstance(e, Compacted) for e in events)


async def test_side_question_with_image_uses_the_vision_role(tmp_path, session):
    main = FakeProvider([FakeProvider.text("main answer")])
    vision = FakeProvider([FakeProvider.text("a dog")])
    agent = Agent(
        main,
        "text-only",
        tools=[],
        workspace=Workspace(tmp_path),
        roles=ModelRoles(vision=(vision, "seeing")),
        model_overrides=OVERRIDES,
    )
    [e async for e in agent.run(session, "main question")]
    answer = await agent.side_question(
        session, "btw what is this?", images=[Image("image/png", "AAA")]
    )
    assert answer.text == "a dog" and answer.notice == ""
    sent = vision.requests[0]
    assert sent.model == "seeing" and isinstance(sent.messages[-1].content[1], Image)
    assert not main.requests[1:]


async def test_side_question_images_without_vision_set_a_notice(tmp_path, session):
    main = FakeProvider([FakeProvider.text("main answer"), FakeProvider.text("sure, text only")])
    agent = Agent(
        main, "text-only", tools=[], workspace=Workspace(tmp_path), model_overrides=OVERRIDES
    )
    [e async for e in agent.run(session, "main question")]
    answer = await agent.side_question(session, "what is this?", images=[Image("image/png", "AAA")])
    assert answer.text == "sure, text only" and "images" in answer.notice
