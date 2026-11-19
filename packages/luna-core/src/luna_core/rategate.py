"""A process-wide request rate gate: token bucket, 429 back-off, slow recovery.

Every team model call goes through one :class:`RateGate` (see
:func:`shared_gate`), so a long autonomous run cannot hammer a provider:
the starting rate is low, a 429 halves it and pauses everybody for
``Retry-After``, and the rate climbs back only after ten quiet minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from luna_core.errors import ProviderError
from luna_core.events import ModelEvent
from luna_core.providers.base import ModelRequest, Provider

#: Starting rate when nothing is configured (requests per minute).
DEFAULT_RPM = 30.0
#: The gate never goes below this many requests per minute.
MIN_RPM = 1.0
#: Pause after a 429 that carries no ``Retry-After`` (seconds).
DEFAULT_BACKOFF = 10.0
#: Quiet time after the last 429 before the rate starts climbing back (seconds).
RECOVER_AFTER = 600.0
#: While recovering, the rate climbs by this share of the target per quiet minute.
RECOVER_STEP = 0.1


class RateGate:
    """A token bucket shared by every model request of the process.

    ``acquire`` waits for a token. ``penalize`` (a 429) pauses everybody for
    ``Retry-After`` seconds and halves the rate. After ``RECOVER_AFTER`` quiet
    seconds the rate climbs back to the target by ``RECOVER_STEP`` of it per
    minute. ``listeners`` are called with the new rate whenever it changes.
    """

    def __init__(
        self,
        rpm: float = DEFAULT_RPM,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if rpm <= 0:
            raise ValueError(f"rpm must be > 0, got {rpm!r}")
        self.target = float(rpm)
        self._rate = float(rpm)
        self._clock = clock
        self._sleep = sleep
        self._stamp = clock()
        self._tokens = self._capacity()
        self._blocked_until = 0.0
        self._recover_from: float | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self.listeners: list[Callable[[float], None]] = []

    @property
    def rpm(self) -> float:
        """The current rate in requests per minute."""
        return self._rate

    def set_target(self, rpm: float) -> None:
        """Change the configured rate (a lower one applies at once)."""
        if rpm <= 0:
            raise ValueError(f"rpm must be > 0, got {rpm!r}")
        self.target = float(rpm)
        if self._recover_from is None or self._rate > self.target:
            self._set_rate(self.target)

    def _capacity(self) -> float:
        return max(1.0, self._rate / 10)

    def _set_rate(self, rpm: float) -> None:
        if rpm == self._rate:
            return
        self._rate = rpm
        self._tokens = min(self._tokens, self._capacity())
        for fn in list(self.listeners):
            with contextlib.suppress(Exception):
                fn(rpm)

    def _recover(self, now: float) -> None:
        if self._recover_from is None or now < self._recover_from:
            return
        minutes = (now - self._recover_from) / 60
        self._recover_from = now
        rate = min(self.target, self._rate + self.target * RECOVER_STEP * minutes)
        if rate >= self.target:
            self._recover_from = None
        self._set_rate(rate)

    def _refill(self, now: float) -> None:
        self._recover(now)
        elapsed = max(0.0, now - self._stamp)
        self._stamp = now
        self._tokens = min(self._capacity(), self._tokens + elapsed * self._rate / 60)

    def _get_lock(self) -> asyncio.Lock:
        # One gate lives across event loops (tests, the server's restarts).
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def acquire(self) -> None:
        """Wait until one more request may go out."""
        async with self._get_lock():
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    await self._sleep(self._blocked_until - now)
                    continue
                self._refill(now)
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await self._sleep((1 - self._tokens) * 60 / self._rate)

    def penalize(self, retry_after: float | None = None) -> None:
        """Record a 429: pause for ``retry_after`` (or the default) and halve the rate."""
        now = self._clock()
        self._refill(now)
        wait = retry_after if retry_after and retry_after > 0 else DEFAULT_BACKOFF
        self._blocked_until = max(self._blocked_until, now + wait)
        self._recover_from = now + RECOVER_AFTER
        self._set_rate(max(MIN_RPM, self._rate / 2))


class GatedProvider:
    """A provider whose every request first passes the gate.

    A 429 before any output penalizes the gate and is retried (up to
    ``attempts`` requests in all); a 429 after output started, or the last
    one, propagates. Other errors pass through untouched.
    """

    def __init__(self, inner: Provider, gate: RateGate, *, attempts: int = 5) -> None:
        self.inner = inner
        self.gate = gate
        self.attempts = attempts
        self.name = inner.name

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream the inner provider's reply once the gate lets the request through."""
        for attempt in range(1, self.attempts + 1):
            await self.gate.acquire()
            started = False
            try:
                async for event in self.inner.stream(request):
                    started = True
                    yield event
                return
            except ProviderError as exc:
                if exc.kind != "rate_limit":
                    raise
                self.gate.penalize(exc.retry_after)
                if started or attempt == self.attempts:
                    raise


_SHARED: RateGate | None = None


def shared_gate(rpm: float | None = None) -> RateGate:
    """Return the process-wide gate, creating it (or retargeting it to ``rpm``)."""
    global _SHARED
    if _SHARED is None:
        _SHARED = RateGate(rpm or DEFAULT_RPM)
    elif rpm is not None:
        _SHARED.set_target(rpm)
    return _SHARED


def gated(provider: Provider, gate: RateGate) -> Provider:
    """Wrap ``provider`` with ``gate`` unless it is already gated."""
    return provider if isinstance(provider, GatedProvider) else GatedProvider(provider, gate)


__all__ = ["GatedProvider", "RateGate", "gated", "shared_gate"]
