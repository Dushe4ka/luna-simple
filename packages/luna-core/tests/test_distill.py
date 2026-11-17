"""Distill: journal → model → candidate profiles, checked by code."""

import json

import pytest
from luna_core.assemblies import RunRecord
from luna_core.distill import (
    INPUT_CHARS,
    Candidate,
    DistillError,
    check_candidate,
    parse_candidates,
    propose,
    render_journal,
    should_disable,
)
from luna_core.errors import ProviderError
from luna_core.providers.fake import FakeProvider


def rec(
    i,
    *,
    ok=True,
    workdir="/w",
    tools=("read_file", "write_file"),
    mutating=(),
    skills=(),
    prompt="Write a SQL migration",
    task="add column",
):
    return RunRecord(
        id=i,
        ts=float(i),
        workdir=workdir,
        parent_session="p",
        child_session=f"c{i}",
        base_profile="general-purpose",
        dynamic=True,
        spec={
            "name": "general-purpose",
            "prompt": prompt,
            "tools": list(tools),
            "skills": list(skills),
            "model": "fast",
            "context": "",
        },
        task_head=task,
        ok=ok,
        cancelled=False,
        hit_max_steps=False,
        steps=3,
        cost=0.0,
        mutating=tuple(mutating),
    )


def cand(runs, *, name="migration-writer", tools=("read_file",), skills=()):
    return Candidate(
        name,
        "Writes migrations",
        "You write SQL migrations.",
        tuple(tools),
        tuple(skills),
        "fast",
        "",
        30,
        tuple(runs),
    )


def by_id(records):
    return {r.id: r for r in records}


PAYLOAD = {
    "profiles": [
        {
            "name": "migration-writer",
            "description": "Writes migrations",
            "prompt": "You write SQL migrations.",
            "tools": ["read_file"],
            "skills": [],
            "model": "fast",
            "context": "",
            "max_steps": 20,
            "runs": [1, 2, 3],
        }
    ]
}


def test_parse_plain_fenced_and_prose():
    plain = json.dumps(PAYLOAD)
    fenced = f"Here you go:\n```json\n{plain}\n```\nDone."
    for text in (plain, fenced):
        (c,) = parse_candidates(text)
        assert c.name == "migration-writer" and c.tools == ("read_file",)
        assert c.runs == (1, 2, 3) and c.max_steps == 20


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no json here",
        "[1, 2]",
        '{"profiles": "nope"}',
        "{broken",
        '{"profiles": [{"name": 5}]}',
    ],
)
def test_parse_garbage_gives_nothing(text):
    assert parse_candidates(text) == []


def test_parse_skips_bad_items_keeps_good():
    bad = dict(PAYLOAD["profiles"][0], name="Bad Name!")
    no_prompt = dict(PAYLOAD["profiles"][0], name="x-ok", prompt="")
    wrong_tools = dict(PAYLOAD["profiles"][0], name="y-ok", tools="read_file")
    wrong_runs = dict(PAYLOAD["profiles"][0], name="z-ok", runs=["1"])
    huge_steps = dict(PAYLOAD["profiles"][0], name="w-ok", max_steps=10_000)
    text = json.dumps(
        {"profiles": [bad, no_prompt, wrong_tools, wrong_runs, huge_steps, PAYLOAD["profiles"][0]]}
    )
    assert [c.name for c in parse_candidates(text)] == ["migration-writer"]


def test_thresholds_five_runs_seventy_percent():
    runs = [rec(i) for i in range(1, 6)]  # 5/5
    assert check_candidate(cand(range(1, 6)), by_id(runs), taken={}, allow_write=False).ok
    four = check_candidate(cand(range(1, 5)), by_id(runs), taken={}, allow_write=False)
    assert not four.ok and "5" in four.reason
    mixed = [rec(i, ok=i <= 7) for i in range(1, 11)]  # 7/10 = 70 %
    assert check_candidate(cand(range(1, 11)), by_id(mixed), taken={}, allow_write=False).ok
    worse = [rec(i, ok=i <= 6) for i in range(1, 11)]  # 6/10
    v = check_candidate(cand(range(1, 11)), by_id(worse), taken={}, allow_write=False)
    assert not v.ok and "70" in v.reason


def test_invented_run_ids_do_not_count():
    runs = [rec(i) for i in range(1, 5)]
    v = check_candidate(cand([1, 2, 3, 4, 99, 100]), by_id(runs), taken={}, allow_write=False)
    assert not v.ok and v.runs == (1, 2, 3, 4)


def test_tools_must_come_from_the_runs():
    runs = [rec(i, tools=("read_file",)) for i in range(1, 6)]
    v = check_candidate(
        cand(range(1, 6), tools=("read_file", "mcp__x__y")), by_id(runs), taken={}, allow_write=True
    )
    assert not v.ok and "mcp__x__y" in v.reason


def test_forbidden_tool_rejected():
    runs = [rec(i, tools=("read_file", "task")) for i in range(1, 6)]
    v = check_candidate(cand(range(1, 6), tools=("task",)), by_id(runs), taken={}, allow_write=True)
    assert not v.ok


def test_skills_must_come_from_the_runs():
    runs = [rec(i, skills=("sql",)) for i in range(1, 6)]
    ok = check_candidate(
        cand(range(1, 6), skills=("sql",)), by_id(runs), taken={}, allow_write=False
    )
    bad = check_candidate(
        cand(range(1, 6), skills=("pdf",)), by_id(runs), taken={}, allow_write=False
    )
    assert ok.ok and not bad.ok


def test_manual_name_is_protected_auto_name_may_update():
    runs = [rec(i) for i in range(1, 6)]
    manual = check_candidate(
        cand(range(1, 6)), by_id(runs), taken={"migration-writer": False}, allow_write=False
    )
    auto = check_candidate(
        cand(range(1, 6)), by_id(runs), taken={"migration-writer": True}, allow_write=False
    )
    assert not manual.ok and "hand-written" in manual.reason
    assert auto.ok


def test_write_ceiling():
    runs = [
        rec(i, tools=("read_file", "write_file", "mcp__db__q"), mutating=("mcp__db__q",))
        for i in range(1, 6)
    ]
    for tools in (("write_file",), ("mcp__db__q",)):
        denied = check_candidate(
            cand(range(1, 6), tools=tools), by_id(runs), taken={}, allow_write=False
        )
        allowed = check_candidate(
            cand(range(1, 6), tools=tools), by_id(runs), taken={}, allow_write=True
        )
        assert not denied.ok and "allow_write" in denied.reason
        assert allowed.ok and allowed.writes


def test_workdirs_reported():
    runs = [rec(i, workdir="/a" if i % 2 else "/b") for i in range(1, 6)]
    v = check_candidate(cand(range(1, 6)), by_id(runs), taken={}, allow_write=False)
    assert v.workdirs == ("/a", "/b") and v.successes == 5


def test_to_spec_is_auto():
    spec = cand([1]).to_spec(unsafe=False, version=3)
    assert spec.auto and spec.version == 3 and spec.isolation == "auto"
    assert spec.tools == ("read_file",) and spec.model == "fast"


def test_render_journal_has_no_answers_and_is_capped():
    text = render_journal([rec(i, prompt="P" * 5000) for i in range(1, 300)])
    assert len(text) <= INPUT_CHARS
    first = json.loads(text.splitlines()[0])
    assert set(first) == {
        "id",
        "workdir",
        "base",
        "dynamic",
        "success",
        "task",
        "prompt",
        "tools",
        "skills",
        "model",
        "context",
    }
    # the newest records survive the cap
    assert json.loads(text.splitlines()[-1])["id"] == 299


async def test_propose_one_request():
    provider = FakeProvider([FakeProvider.text(json.dumps(PAYLOAD))])
    out = await propose(provider, "fast-model", [rec(1), rec(2)])
    assert [c.name for c in out] == ["migration-writer"]
    (request,) = provider.requests
    assert request.model == "fast-model" and request.max_tokens == 4000


async def test_propose_empty_journal_sends_nothing():
    provider = FakeProvider([])
    assert await propose(provider, "m", []) == []
    assert provider.requests == []


async def test_propose_provider_error_is_distill_error():
    class Broken:
        name = "broken"

        async def stream(self, request):
            raise ProviderError("429 slow down")
            yield  # pragma: no cover

    with pytest.raises(DistillError, match="429"):
        await propose(Broken(), "m", [rec(1)])


def test_should_disable():
    assert not should_disable([rec(i, ok=False) for i in range(4)])  # < 5 runs: too early
    assert should_disable([rec(i, ok=i < 2) for i in range(5)])  # 2/5
    assert not should_disable([rec(i, ok=i < 5) for i in range(10)])  # 5/10 = 50 %
    assert should_disable([rec(i, ok=i < 4) for i in range(10)])  # 4/10
    # only the newest 10 count
    newest_good = [rec(i) for i in range(10)] + [rec(i, ok=False) for i in range(10, 30)]
    assert not should_disable(newest_good)
