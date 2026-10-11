import pytest
from luna_core.compaction import Compactor, estimate_tokens
from luna_core.errors import ProviderError
from luna_core.events import Compacted, Notice, TurnDone
from luna_core.hooks import HookResult, Hooks
from luna_core.loop import Agent
from luna_core.messages import Message, Text, message_to_dict
from luna_core.providers.base import ModelRequest
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session
from luna_core.usage import Usage
from luna_core.workspace import Workspace


@pytest.fixture
def session(tmp_path):
    log = EventLog(tmp_path / "s.db")
    s = Session.create(log, str(tmp_path), undo=False)
    for i in range(6):
        s.append("turn_started", {"turn_id": f"t{i}"})
        s.append("user", {"message": message_to_dict(Message.user(f"question {i} " + "x" * 400))})
        s.append(
            "assistant",
            {
                "message": message_to_dict(Message.assistant([Text(f"answer {i} " + "y" * 400)])),
                "usage": Usage().to_dict(),
            },
        )
    yield s
    log.close()


def make_agent(tmp_path, script, window=100_000, hooks=None):
    return Agent(
        FakeProvider(script),
        "tiny-model",
        workspace=Workspace(tmp_path),
        tools=[],
        hooks=hooks,
        model_overrides={"tiny-model": {"window": window}},
    )


def test_estimate_tokens_counts_text_and_images():
    req = ModelRequest(model="m", system="s" * 400, messages=[Message.user("u" * 400)])
    assert 190 <= estimate_tokens(req) <= 260


async def test_manual_compact_keeps_last_turns(tmp_path, session):
    summarizer = FakeProvider([FakeProvider.text("SUMMARY of turns 0-1")])
    agent = make_agent(tmp_path, [])
    events = [
        e
        async for e in Compactor(keep_turns=4, provider=summarizer, model="sum-model").compact(
            agent, session
        )
    ]
    assert events == [Compacted("SUMMARY of turns 0-1", 4)]
    msgs = session.messages()
    assert "SUMMARY of turns 0-1" in msgs[0].text and msgs[1].text.startswith("question 2")
    sent = summarizer.requests[0]
    assert (
        sent.model == "sum-model"
        and "question 0" in sent.messages[0].text
        and "question 2" not in sent.messages[0].text
    )


async def test_second_compaction_folds_in_the_previous_summary(tmp_path, session):
    summarizer = FakeProvider([FakeProvider.text("S1"), FakeProvider.text("S2")])
    agent = make_agent(tmp_path, [])
    c = Compactor(keep_turns=4, provider=summarizer)
    [e async for e in c.compact(agent, session)]
    c.keep_turns = 1
    [e async for e in c.compact(agent, session)]
    assert "S1" in summarizer.requests[1].messages[0].text
    assert session.messages()[0].text.count("S2") == 1 and session.messages()[1].text.startswith(
        "question 5"
    )


async def test_nothing_to_compact(tmp_path, session):
    events = [
        e
        async for e in Compactor(keep_turns=10, provider=FakeProvider([])).compact(
            make_agent(tmp_path, []), session
        )
    ]
    assert events == [Notice("info", "nothing to compact yet")]


async def test_pre_compact_hook_can_block(tmp_path, session):
    hooks = Hooks()
    hooks.add("pre_compact", lambda p: _r(HookResult(block=True, message="not now")))
    events = [
        e
        async for e in Compactor(provider=FakeProvider([])).compact(
            make_agent(tmp_path, [], hooks=hooks), session
        )
    ]
    assert isinstance(events[0], Notice) and "not now" in events[0].text


async def _r(v):
    return v


async def test_failure_leaves_history_alone(tmp_path, session):
    before = [m.text for m in session.messages()]
    bad = FakeProvider([[ProviderError("boom", "server")]])
    events = [e async for e in Compactor(provider=bad).compact(make_agent(tmp_path, []), session)]
    assert events[0].level == "warn" and "compaction failed" in events[0].text
    assert [m.text for m in session.messages()] == before


async def test_loop_compacts_automatically_at_the_threshold(tmp_path, session):
    agent = make_agent(
        tmp_path, [FakeProvider.text("SUMMARY"), FakeProvider.text("fine")], window=1000
    )
    agent.compactor = Compactor(threshold=0.8, keep_turns=2)
    events = [e async for e in agent.run(session, "next")]
    assert any(isinstance(e, Compacted) for e in events) and isinstance(events[-1], TurnDone)
    assert "SUMMARY" in agent.provider.requests[1].messages[0].text
