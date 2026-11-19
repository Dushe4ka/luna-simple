"""The process-wide rate gate (token bucket, 429 back-off, recovery)."""

import pytest
from luna_core import rategate
from luna_core.errors import ProviderError
from luna_core.events import TextDelta
from luna_core.providers.base import ModelRequest
from luna_core.providers.fake import FakeProvider
from luna_core.rategate import GatedProvider, RateGate, gated, shared_gate

REQUEST = ModelRequest(model="m", system="", messages=[])


class Clock:
    """Fake monotonic clock whose sleep only moves time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def gate(rpm: float = 30.0) -> tuple[RateGate, Clock]:
    clock = Clock()
    return RateGate(rpm, clock=clock, sleep=clock.sleep), clock


async def test_tokens_space_requests_at_the_rate():
    g, clock = gate(60)  # bucket of 6, one token per second
    for _ in range(6):
        await g.acquire()
    assert clock.slept == []
    await g.acquire()
    assert clock.slept == [pytest.approx(1.0)]


async def test_429_waits_retry_after_and_halves_the_rate():
    g, clock = gate(30)
    seen: list[float] = []
    g.listeners.append(seen.append)
    g.penalize(7.0)
    assert g.rpm == 15 and seen == [15]
    await g.acquire()
    assert clock.slept[0] == pytest.approx(7.0)


async def test_missing_retry_after_uses_the_default_backoff():
    g, clock = gate(30)
    g.penalize(None)
    await g.acquire()
    assert clock.slept[0] == pytest.approx(rategate.DEFAULT_BACKOFF)


async def test_rate_recovers_slowly_after_ten_quiet_minutes():
    g, clock = gate(30)
    g.penalize(1.0)
    clock.now += 599
    await g.acquire()
    assert g.rpm == 15
    clock.now += 61  # one minute past the quiet window
    await g.acquire()
    assert g.rpm == pytest.approx(18.0)
    clock.now += 600
    await g.acquire()
    assert g.rpm == 30


async def test_gated_provider_retries_after_429_and_penalizes():
    g, clock = gate(30)
    inner = FakeProvider(
        [[ProviderError("slow down", "rate_limit", 429, retry_after=7.0)], FakeProvider.text("hi")]
    )
    events = [e async for e in GatedProvider(inner, g).stream(REQUEST)]
    assert any(isinstance(e, TextDelta) and e.text == "hi" for e in events)
    assert g.rpm == 15 and len(inner.requests) == 2
    assert any(abs(s - 7.0) < 1e-6 for s in clock.slept)


async def test_gated_provider_gives_up_after_its_attempts():
    g, _clock = gate(30)
    script = [[ProviderError("slow", "rate_limit", 429, retry_after=1.0)] for _ in range(3)]
    provider = GatedProvider(FakeProvider(script), g, attempts=3)
    with pytest.raises(ProviderError):
        async for _ in provider.stream(REQUEST):
            pass
    assert g.rpm == pytest.approx(30 / 8)


async def test_other_errors_pass_through_without_penalty():
    g, _clock = gate(30)
    provider = GatedProvider(FakeProvider([[ProviderError("bad key", "auth", 401)]]), g)
    with pytest.raises(ProviderError):
        async for _ in provider.stream(REQUEST):
            pass
    assert g.rpm == 30


def test_shared_gate_is_one_object_and_takes_a_new_target(monkeypatch):
    monkeypatch.setattr(rategate, "_SHARED", None)
    first = shared_gate()
    assert first.target == rategate.DEFAULT_RPM
    second = shared_gate(12)
    assert second is first and first.target == 12


def test_gated_wraps_once():
    g, _clock = gate()
    inner = FakeProvider([])
    once = gated(inner, g)
    assert isinstance(once, GatedProvider) and gated(once, g) is once
    assert once.name == inner.name


def test_rpm_must_be_positive():
    with pytest.raises(ValueError):
        RateGate(0)
