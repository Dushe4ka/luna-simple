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


async def test_compact_counts_distinct_turns_not_user_role_entries(tmp_path):
    """Steering/turn_end-continuation messages share a turn_id; they must not count as turns."""
    log = EventLog(tmp_path / "steer.db")
    s = Session.create(log, str(tmp_path), undo=False)
    for i in range(6):
        s.append("turn_started", {"turn_id": f"t{i}"})
        s.append("user", {"message": message_to_dict(Message.user(f"question {i} " + "x" * 400))})
        if i in (4, 5):  # extra user-role entry mid-turn, same turn_id (steer / turn_end hook)
            s.append("user", {"message": message_to_dict(Message.user(f"steer {i}"))})
        s.append(
            "assistant",
            {
                "message": message_to_dict(Message.assistant([Text(f"answer {i} " + "y" * 400)])),
                "usage": Usage().to_dict(),
            },
        )
    summarizer = FakeProvider([FakeProvider.text("SUMMARY of turns 0-1")])
    agent = make_agent(tmp_path, [])
    events = [e async for e in Compactor(keep_turns=4, provider=summarizer).compact(agent, s)]
    assert events == [Compacted("SUMMARY of turns 0-1", 4)]
    msgs = s.messages()
    assert "SUMMARY of turns 0-1" in msgs[0].text
    assert msgs[1].text.startswith("question 2")
    texts = [m.text for m in msgs]
    assert "steer 4" in texts and "steer 5" in texts
    log.close()


async def test_automatic_compaction_backs_off_after_failure(tmp_path, session):
    bad = FakeProvider([[ProviderError("boom", "server")]])
    agent = make_agent(tmp_path, [], window=1000)
    compactor = Compactor(threshold=0.8, keep_turns=2, provider=bad)

    events1 = [e async for e in compactor.maybe_compact(agent, session)]
    assert any(isinstance(e, Notice) and e.level == "warn" for e in events1)
    assert len(bad.requests) == 1

    events2 = [e async for e in compactor.maybe_compact(agent, session)]
    events3 = [e async for e in compactor.maybe_compact(agent, session)]
    assert events2 == [] and events3 == []
    assert len(bad.requests) == 1

    for i in range(6, 8):
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
    bad.script.append([ProviderError("boom again", "server")])
    events4 = [e async for e in compactor.maybe_compact(agent, session)]
    assert any(isinstance(e, Notice) and e.level == "warn" for e in events4)
    assert len(bad.requests) == 2


async def test_automatic_nothing_to_compact_is_silent(tmp_path, session):
    agent = make_agent(tmp_path, [], window=1000)
    compactor = Compactor(threshold=0.8, keep_turns=10, provider=FakeProvider([]))
    for _ in range(3):
        events = [e async for e in compactor.maybe_compact(agent, session)]
        assert events == []


async def test_oversized_transcript_is_trimmed(tmp_path, session):
    summarizer = FakeProvider([FakeProvider.text("SUMMARY")])
    agent = make_agent(tmp_path, [])
    agent.model_overrides["tiny-sum"] = {"window": 200}
    events = [
        e
        async for e in Compactor(keep_turns=0, provider=summarizer, model="tiny-sum").compact(
            agent, session
        )
    ]
    assert isinstance(events[0], Compacted)
    sent_text = summarizer.requests[0].messages[0].text
    assert sent_text.startswith("[earlier part omitted]")
    assert len(sent_text) <= 560


async def _auto(compactor, agent, session):
    return [e async for e in compactor.maybe_compact(agent, session, window=1500)]


async def test_always_blocking_pre_compact_hook_backs_off(tmp_path, session):
    calls = []

    async def block(payload):
        calls.append(payload)
        return HookResult(block=True, message="not now")

    hooks = Hooks()
    hooks.add("pre_compact", block)
    agent = make_agent(tmp_path, [], hooks=hooks)
    compactor = Compactor(provider=FakeProvider([]))
    first = await _auto(compactor, agent, session)
    assert len(first) == 1 and "not now" in first[0].text
    # The following steps/turns neither run the hook nor repeat the notice.
    for _ in range(5):
        session.append("user", {"message": message_to_dict(Message.user("more"))})
        assert await _auto(compactor, agent, session) == []
    assert len(calls) == 1
    # A manual /compact is never suppressed.
    manual = [e async for e in compactor.compact(agent, session)]
    assert manual and "not now" in manual[0].text and len(calls) == 2
    # After enough growth the hook is consulted again (one notice).
    for _ in range(30):
        session.append("user", {"message": message_to_dict(Message.user("more"))})
    again = await _auto(compactor, agent, session)
    assert len(again) == 1 and len(calls) == 3


async def test_blocked_marker_is_cleared_by_a_successful_compaction(tmp_path, session):
    state = {"block": True}

    async def gate(payload):
        return HookResult(block=state["block"], message="not now")

    hooks = Hooks()
    hooks.add("pre_compact", gate)
    agent = make_agent(tmp_path, [], hooks=hooks)
    compactor = Compactor(provider=FakeProvider([FakeProvider.text("SUMMARY")]))
    for _ in range(40):
        session.append("user", {"message": message_to_dict(Message.user("more"))})
    assert len(await _auto(compactor, agent, session)) == 1  # blocked, marker set
    assert "compaction_blocked" in session.state
    state["block"] = False
    manual = [e async for e in compactor.compact(agent, session)]
    assert any(isinstance(e, Compacted) for e in manual)
    assert "compaction_blocked" not in session.state


async def test_blocked_marker_with_shrunk_history_counts_as_expired(tmp_path, session):
    agent = make_agent(tmp_path, [])
    compactor = Compactor(provider=FakeProvider([FakeProvider.text("S")]))
    session.state["compaction_blocked"] = len(session.entries()) + 80  # history shrank (undo)
    events = await _auto(compactor, agent, session)
    assert any(isinstance(e, Compacted) for e in events)


async def test_blocked_backoff_is_overridden_near_the_window(tmp_path, session):
    calls = []

    async def block(payload):
        calls.append(1)
        return HookResult(block=True, message="no")

    hooks = Hooks()
    hooks.add("pre_compact", block)
    agent = make_agent(tmp_path, [], hooks=hooks)
    compactor = Compactor(provider=FakeProvider([]))
    # ~1200 tokens estimated: over the 0.8 threshold of 1000, below 0.95 of 1300.
    assert len(await _auto(compactor, agent, session)) == 1
    assert await _auto(compactor, agent, session) == []
    # Window so small that the context is at/over 95%: the hook is asked again.
    tight = [e async for e in compactor.maybe_compact(agent, session, window=1100)]
    assert len(calls) == 2 and tight
