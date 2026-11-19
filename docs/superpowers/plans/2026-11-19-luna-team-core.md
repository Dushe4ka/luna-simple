# Luna Team Mode — Core (plan 2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `luna-core` a working, tested team engine — a lead on the `strong` role plans read-only, members work tasks from a shared board in their own git worktrees, finished work is merged serially into `luna/team/<id>`, limits and a soft stop hold, a run survives a restart, and every run ends with `report.md`.

**Architecture:** A new package `luna_core.team` (board/messages store in `sessions.db`, plan schema + planner, permission profile, git plumbing, tools, report, keep-awake, runner) plus `luna_core.rategate` (process-wide token bucket around every team model call). The runner is one asyncio loop per run: it claims ready tasks for idle members (atomic SQL), runs each member as a `luna_core.loop.Agent` turn, merges under an `asyncio.Lock`, and wakes the lead only when something changed. The host app wiring (`luna team`, server endpoints, TUI) is plans 2b/2c; here the only app change is the `strong` role.

**Tech Stack:** Python 3.11+, asyncio, sqlite3 (WAL, `BEGIN IMMEDIATE`), git CLI, pytest + pytest-asyncio (`asyncio_mode = "auto"`), `FakeProvider`.

**Spec:** `docs/superpowers/specs/2026-11-16-luna-dynamic-subagents-teams-design.md` — Part 2 «Режим команды» (2.2–2.8, 2.11) and «Риски». Plans 1 and 1b of that spec are merged (`df71fc7`, `4d63cc9`, follow-ups `2c3ebf5`).

## Global Constraints

- Python 3.11+, PEP 8 / PEP 257, `uv run ruff check .` and `uv run ruff format --check .` clean.
- Tests never touch the network and never use real keys: `FakeProvider` / the `TeamProvider` test helper only.
- Model IDs never appear in core logic; tests use fake names (`main-model`, `lead-model`).
- Role resolution: `strong` → `plan` → the default model (user decision, 2026-11-19).
- The team never touches the user's working tree, the user's current branch or `main`. It writes only branches `luna/team/<id>` and `luna/teamwork/<id>/<task>`, worktrees `.luna/worktrees/team-*`, and files under `.luna/teams/<id>/`.
- `deny` rules from `permissions.toml` always beat the run's permission profile.
- Everything outside the profile is denied automatically (no human is asked during a run) and recorded as a `team_denied` event.
- Limits (defaults): `max_concurrent` 4, `max_members` 8, depth 1, `member_steps` 30, `lead_steps` 60, `rpm` 30 (adaptive), `budget` none, `wall_clock` 2 h (soft stop).
- Rate gate: token bucket, honours `Retry-After` on 429 and halves the rate, climbs back after 10 quiet minutes. Starting rate is 30 rpm; only an explicit setting raises it.
- Stall detector: 3 lead wakes with an unchanged board → "stop discussing" signal; 3 more → run ends `stalled`.
- Soft stop (time, budget, cancel): no new tasks; running members get at most one more step to save; then the report.
- Report headline is exactly one of: `ГОТОВО` | `ЧАСТИЧНО n/m` | `ОСТАНОВЛЕНО (время)` / `(бюджет)` / `(отмена)` | `ЗАСТОЙ` | `ОШИБКА`.
- Cross-platform: paths via `pathlib`, commands via `luna_core.shell.run_sync`, git via `luna_core.subagents._git`.

## Rulings made while planning (spec gaps)

- **Fifth table `team_members`.** The spec names four tables; per-member steps, cost and busy time need their own rows. Cost if wrong: drop the table, fold into events.
- **No `board_claim` tool.** One member turn works exactly one task in its own worktree, so the runner claims the task for the member with the atomic `TeamStore.claim_next` before the turn. Members get `board_list`, `board_update`, `send_message`, `read_messages`. Cost if wrong: add the tool later.
- **File writes inside the member's own worktree are always allowed for `code` tasks.** The worktree is the isolation; the profile governs `execute` and mutating MCP tools. `research` members get no writing tools.
- **Member branches are `luna/teamwork/<id>/<task>`.** Git cannot hold both `luna/team/<id>` and `luna/team/<id>/T1` as refs.
- **`member_steps` is per task attempt.**
- **Soft stop grants "the step in flight plus one":** `max_steps = min(max_steps, completed + 2)`. The runner cannot tell whether a model call is in flight.
- **Out of scope for 2a (plans 2b/2c):**
  - `luna team …` CLI, `/teams` endpoints, SSE, `/team` command, TUI screens;
  - `[team]` config parsing and wiring `--sandbox docker` (the runner already takes `host.sandbox`);
  - completion notifications;
  - putting the shared rate gate in front of ordinary sessions.

## Review Focus

1. **The server dies mid-turn**, so the asyncio task is cancelled while a member's request hangs. Expected: on the next `run()` the in-progress task goes back to `pending`, its worktree is committed and removed, and the report says «прервано в HH:MM, продолжено». Pinned in Task 12, `test_run_survives_a_restart`.
2. **The user has uncommitted and untracked files.** Expected: the team branches from `HEAD` and never changes or sees them. Pinned in Task 6, `test_start_creates_the_team_branch_and_leaves_the_user_tree_alone`, and Task 10, `test_team_merges_code_writes_notes_and_reports`.
3. **The lead submits a plan with a dependency cycle, an unknown member or `execute:*`.** Expected: refused with a reason the lead can act on, and the lead retries. Pinned in Task 4, `test_bad_plans_are_refused_with_a_reason` and `test_planner_explores_read_only_and_returns_the_submitted_plan`.
4. **A member "resolves" a conflict but leaves `<<<<<<<` markers.** Expected: never merged; the task is retried. Pinned in Task 6, `test_conflicting_work_is_reported_and_resolved_in_a_merge_tree`, and Task 10, conflict test.
5. **The budget runs out in the middle of a turn.** Expected: no new task starts, finished work is still merged, and the run ends «ОСТАНОВЛЕНО (бюджет)». Pinned in Task 11, `test_budget_stops_softly`.

## File Structure

| File | Responsibility |
|---|---|
| `packages/luna-core/src/luna_core/roles.py` (modify) | `strong` role with fallback to `plan` |
| `luna/config/config.py` (modify) | read `[model] strong` |
| `packages/luna-core/src/luna_core/rategate.py` (create) | `RateGate`, `GatedProvider`, `shared_gate`, `gated` |
| `packages/luna-core/src/luna_core/team/__init__.py` (create) | public names of the team package |
| `…/team/settings.py` (create) | `TeamSettings` (limits) |
| `…/team/board.py` (create) | `TeamStore`: runs, tasks, members, events; atomic claim |
| `…/team/messages.py` (create) | `Mailbox`: direct messages, lead-only broadcast |
| `…/team/plan.py` (create) | `TeamPlan` schema, `parse_plan`, `Planner` (read-only lead) |
| `…/team/permissions.py` (create) | `TeamPolicy`, `default_allow` |
| `…/team/git.py` (create) | `TeamGit`: team branch, member worktrees, serial merge, verify |
| `…/team/report.py` (create) | `build_report`, `write_report`, `headline` |
| `…/team/inhibit.py` (create) | `keep_awake` (caffeinate / systemd-inhibit / SetThreadExecutionState) |
| `…/team/tools.py` (create) | member and lead board/message tools |
| `…/team/runner.py` (create) | `TeamHost`, `TeamRunner` |
| `packages/luna-core/tests/team_kit.py` (create) | test helpers: routing provider, repo, runner factory |
| `packages/luna-core/tests/test_rategate.py`, `test_team_*.py` (create) | tests |
| `packages/luna-core/src/luna_core/__init__.py`, `tests/test_public_api.py`, `README.md`, `AGENTS.md` (modify) | exports and docs |

---

### Task 1: The `strong` model role

**Files:**
- Modify: `packages/luna-core/src/luna_core/roles.py`
- Modify: `luna/config/config.py:117`
- Modify: `README.md` (the bullet list after the `.luna.toml` example, next to `- [model] fast`)
- Test: `packages/luna-core/tests/test_roles.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `ROLES = ("fast", "plan", "vision", "summarize", "strong")`; `ModelRoles.strong`; `ModelRoles.get("strong", provider, model)` returns strong → plan → default.

- [ ] **Step 1: Write the failing tests**

Append to `packages/luna-core/tests/test_roles.py`:

```python
def test_strong_role_falls_back_to_plan_then_default():
    main, other = FakeProvider([]), FakeProvider([])
    assert ModelRoles().get("strong", main, "big") == (main, "big")
    assert ModelRoles(plan="thinker").get("strong", main, "big") == (main, "thinker")
    assert ModelRoles(plan="thinker", strong="genius").get("strong", main, "big") == (
        main,
        "genius",
    )
    assert ModelRoles(strong=(other, "far")).get("strong", main, "big") == (other, "far")


def test_strong_is_never_used_for_ordinary_turns():
    main = FakeProvider([])
    roles = ModelRoles(strong="genius")
    assert roles.for_turn(main, "big", images=False, plan=False) == (main, "big", None)
    assert roles.for_turn(main, "big", images=False, plan=True) == (main, "big", None)


def test_dynamic_subagents_may_ask_for_the_strong_role():
    from luna_core.dynamic import resolve_spec
    from luna_core.subagents import BUILTIN_SUBAGENTS, DynamicSettings

    spec, dynamic = resolve_spec(
        BUILTIN_SUBAGENTS["researcher"],
        {"model": "strong"},
        parent_tools={"read_file": object()},
        skills=(),
        settings=DynamicSettings(),
    )
    assert dynamic and spec.model == "strong"
```

Append to `tests/test_config.py`:

```python
def test_strong_role_is_read_from_the_model_table(tmp_path):
    (tmp_path / ".luna.toml").write_text(
        '[model]\nname = "m"\nplan = "p"\nstrong = "anthropic:big"\n'
    )
    cfg = load_config(cwd=str(tmp_path), env={})
    assert cfg.roles == {"plan": "p", "strong": "anthropic:big"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_roles.py tests/test_config.py -k strong -v`
Expected: FAIL — `ValueError: unknown role 'strong'`, then `TypeError: ... unexpected keyword argument 'strong'`, then the config test gets `{"plan": "p"}`.

- [ ] **Step 3: Implement**

In `packages/luna-core/src/luna_core/roles.py` replace the `ROLES` line and the class head and `get`:

```python
RoleTarget = str | tuple[Provider, str]
ROLES = ("fast", "plan", "vision", "summarize", "strong")
#: A role that is not set borrows another role's model before the default one.
_FALLBACK = {"strong": "plan"}


@dataclass
class ModelRoles:
    """Optional per-role models; unset roles fall back to the default model.

    ``strong`` (the team lead) falls back to ``plan`` first.
    """

    fast: RoleTarget | None = None
    plan: RoleTarget | None = None
    vision: RoleTarget | None = None
    summarize: RoleTarget | None = None
    strong: RoleTarget | None = None

    def get(self, role: str, provider: Provider, model: str) -> tuple[Provider, str]:
        """Return the provider and model for ``role``."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        target = getattr(self, role)
        if target is None:
            fallback = _FALLBACK.get(role)
            if fallback is not None:
                return self.get(fallback, provider, model)
            return provider, model
        if isinstance(target, tuple):
            return target
        return provider, target
```

In `luna/config/config.py` add `from luna_core.roles import ROLES` to the imports (keep isort order) and change line 117:

```python
    for role in ROLES:
```

Then run `grep -rn '"summarize"' luna/ --include=*.py`. Wherever role names are listed for display or validation, add `"strong"` the same way, except `config.py:117`, which now uses `ROLES`. If the grep finds nothing else, change nothing.

In `README.md`, after the `- [model] fast` bullet, add:

```markdown
- `[model] plan` / `[model] strong` — модели ролей. `plan` работает в режиме
  `/plan`; `strong` — самая сильная модель, на ней работает лидер команды
  (`luna team`). Если `strong` не задана, берётся `plan`, а если нет и её —
  основная модель.
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/luna-core/tests/test_roles.py packages/luna-core/tests/test_dynamic.py tests/test_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/roles.py luna/config/config.py README.md packages/luna-core/tests/test_roles.py tests/test_config.py
git commit -m "feat(roles): strong role for the team lead, falls back to plan"
```

---

### Task 2: The rate gate

**Files:**
- Create: `packages/luna-core/src/luna_core/rategate.py`
- Test: `packages/luna-core/tests/test_rategate.py`

**Interfaces:**
- Produces:
  - `RateGate(rpm=30.0, *, clock=time.monotonic, sleep=asyncio.sleep)` with `.rpm`, `.target`, `.listeners: list[Callable[[float], None]]`, `async acquire()`, `penalize(retry_after: float | None = None)`, `set_target(rpm)`;
  - `GatedProvider(inner, gate, *, attempts=5)` (a `Provider`);
  - `shared_gate(rpm: float | None = None) -> RateGate`;
  - `gated(provider, gate) -> Provider`;
  - constants `DEFAULT_RPM = 30.0`, `MIN_RPM = 1.0`, `DEFAULT_BACKOFF = 10.0`, `RECOVER_AFTER = 600.0`, `RECOVER_STEP = 0.1`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_rategate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna_core.rategate'`

- [ ] **Step 3: Implement `rategate.py`**

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/luna-core/tests/test_rategate.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/rategate.py packages/luna-core/tests/test_rategate.py
git commit -m "feat(core): process-wide rate gate with 429 back-off and slow recovery"
```

---

### Task 3: Team settings, board store and mailbox

**Files:**
- Create: `packages/luna-core/src/luna_core/team/__init__.py` (docstring only for now: `"""Team mode: a lead and members working a shared board."""`)
- Create: `packages/luna-core/src/luna_core/team/settings.py`
- Create: `packages/luna-core/src/luna_core/team/board.py`
- Create: `packages/luna-core/src/luna_core/team/messages.py`
- Test: `packages/luna-core/tests/test_team_board.py`

**Interfaces:**
- Produces (`settings.py`): `TeamSettings` (frozen dataclass with fields `max_concurrent=4, max_members=8, member_steps=30, lead_steps=60, rpm=30.0, budget=None, wall_clock=7200.0, verify_command="", verify_timeout=600.0, max_attempts=3, stall_wakes=3, allow_sleep=False, models=()`), `.to_dict()`, `TeamSettings.from_dict(data)`; a bad value raises `ValueError`.
- Produces (`board.py`):
  - constants `TASK_STATUSES`, `TASK_KINDS = ("code", "research")`, `RUN_STATUSES = ("planning", "awaiting_plan", "running", "finished")`, `OUTCOMES = ("done", "partial", "stopped", "stalled", "error")`, `MEMBER_STATUSES`, `LEAD = "lead"`;
  - dataclasses `TeamRun`, `TeamTask`, `TeamMember`, `TeamEvent` (fields as in the code below);
  - `TeamStore(path)` methods:
    - runs: `create_run(*, workdir, task, session_id, settings, budget=None) -> str`, `run(id)`, `runs(*, workdir=None, status=None, limit=100)`, `update_run(id, **fields)`, `delete_run(id)`;
    - tasks: `add_task(run_id, *, title, kind="code", detail="", deps=(), assignee=None, merge_of=None, task_id=None) -> TeamTask`, `tasks(run_id)`, `task(run_id, task_id)`, `ready(run_id)`, `claim(run_id, task_id, member) -> bool`, `claim_next(run_id, member) -> TeamTask | None`, `update_task(run_id, task_id, **fields) -> TeamTask`, `remove_task(run_id, task_id, *, replace_with=())`;
    - members: `add_member(run_id, name, spec) -> TeamMember`, `members(run_id, *, include_retired=True)`, `member(run_id, name)`, `update_member(run_id, name, *, status=None, add_steps=0, add_cost=0.0, add_busy=0.0)`;
    - events: `log_event(run_id, kind, member="", **data) -> TeamEvent`, `events(run_id, *, after=0, kinds=None)`, `subscribe(fn) -> unsubscribe`;
    - `fingerprint(run_id) -> str`, `close()`.
- Produces (`messages.py`): `TeamMessage`; `Mailbox(store)` with `send(run_id, sender, recipient, text) -> TeamMessage`, `broadcast(run_id, sender, text) -> int` (lead only), `unread(run_id, recipient, *, mark=True) -> list[TeamMessage]`, `count_unread(run_id, recipient) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
"""The team board store, settings and mailbox."""

import threading

import pytest
from luna_core.team.board import LEAD, TeamStore
from luna_core.team.messages import Mailbox
from luna_core.team.settings import TeamSettings


@pytest.fixture
def store(tmp_path):
    s = TeamStore(tmp_path / "s.db")
    yield s
    s.close()


def new_run(store: TeamStore) -> str:
    return store.create_run(
        workdir="/w", task="do it", session_id="sess", settings=TeamSettings().to_dict()
    )


def test_settings_validate_and_roundtrip():
    with pytest.raises(ValueError):
        TeamSettings(max_concurrent=0)
    with pytest.raises(ValueError):
        TeamSettings(rpm=0)
    with pytest.raises(ValueError):
        TeamSettings(budget=-1)
    settings = TeamSettings(models=("x",), budget=2.0)
    assert TeamSettings.from_dict(settings.to_dict()) == settings


def test_run_roundtrip(store):
    rid = new_run(store)
    run = store.run(rid)
    assert (run.status, run.task, run.settings["max_concurrent"]) == ("planning", "do it", 4)
    store.update_run(rid, status="running", plan={"goal": "g"}, branch="luna/team/x")
    run = store.run(rid)
    assert (run.status, run.plan, run.branch) == ("running", {"goal": "g"}, "luna/team/x")
    assert [r.id for r in store.runs(status="running")] == [rid]
    with pytest.raises(ValueError):
        store.update_run(rid, status="weird")
    with pytest.raises(ValueError):
        store.update_run(rid, nonsense=1)


def test_tasks_get_ids_and_only_ready_ones_are_claimed(store):
    rid = new_run(store)
    t1 = store.add_task(rid, title="a", kind="code")
    t2 = store.add_task(rid, title="b", kind="research", deps=[t1.id])
    assert (t1.id, t2.id) == ("T1", "T2")
    assert [t.id for t in store.ready(rid)] == ["T1"]
    got = store.claim_next(rid, "dev")
    assert (got.id, got.status, got.assignee, got.attempts) == ("T1", "in_progress", "dev", 1)
    assert store.claim_next(rid, "dev2") is None
    store.update_task(rid, "T1", status="done")
    assert store.claim_next(rid, "dev2").id == "T2"


def test_unknown_dependency_kind_and_duplicate_id_are_refused(store):
    rid = new_run(store)
    with pytest.raises(ValueError, match="unknown dependencies"):
        store.add_task(rid, title="x", deps=["T9"])
    with pytest.raises(ValueError, match="kind"):
        store.add_task(rid, title="x", kind="poem")
    store.add_task(rid, title="x", task_id="A")
    with pytest.raises(ValueError, match="already exists"):
        store.add_task(rid, title="y", task_id="A")
    assert store.add_task(rid, title="z").id == "T1"


def test_assigned_tasks_go_to_their_member_first(store):
    rid = new_run(store)
    store.add_task(rid, title="free")
    store.add_task(rid, title="mine", assignee="bob")
    assert store.claim_next(rid, "bob").title == "mine"
    assert store.claim_next(rid, "amy").title == "free"
    store.add_task(rid, title="bobs", assignee="bob")
    assert store.claim_next(rid, "amy") is None


def test_claim_is_atomic_across_connections(tmp_path):
    a, b = TeamStore(tmp_path / "s.db"), TeamStore(tmp_path / "s.db")
    rid = new_run(a)
    a.add_task(rid, title="only")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def grab(store: TeamStore, who: str) -> None:
        barrier.wait()
        results.append(store.claim(rid, "T1", who))

    threads = [threading.Thread(target=grab, args=(s, n)) for s, n in ((a, "x"), (b, "y"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    a.close()
    b.close()


def test_remove_task_rewires_dependents(store):
    rid = new_run(store)
    store.add_task(rid, title="a")
    store.add_task(rid, title="b", deps=["T1"])
    store.add_task(rid, title="c")
    store.remove_task(rid, "T1", replace_with=["T3"])
    assert store.task(rid, "T1") is None and store.task(rid, "T2").deps == ("T3",)


def test_members_events_and_fingerprint(store):
    rid = new_run(store)
    store.add_task(rid, title="a")
    store.add_member(rid, "dev", {"prompt": "p"})
    with pytest.raises(ValueError, match="already"):
        store.add_member(rid, "dev", {})
    seen = []
    unsubscribe = store.subscribe(seen.append)
    before = store.fingerprint(rid)
    store.log_event(rid, "team_message", "dev", to="lead", text="hi")
    assert store.fingerprint(rid) == before  # chatter does not move the board
    store.claim_next(rid, "dev")
    assert store.fingerprint(rid) != before
    store.update_member(rid, "dev", add_steps=3, add_cost=0.5, add_busy=2.0)
    member = store.member(rid, "dev")
    assert (member.steps, member.cost, member.busy, member.spec) == (3, 0.5, 2.0, {"prompt": "p"})
    unsubscribe()
    store.log_event(rid, "team_task", task="T1")
    assert [e.kind for e in seen] == ["team_message"]
    assert [e.data for e in store.events(rid, kinds=["team_task"])] == [{"task": "T1"}]


def test_mailbox_direct_and_broadcast(store):
    rid = new_run(store)
    box = Mailbox(store)
    store.add_member(rid, LEAD, {"role": "lead"})
    for name in ("amy", "bob"):
        store.add_member(rid, name, {})
    box.send(rid, "amy", "bob", "need T1 api")
    with pytest.raises(ValueError):
        box.send(rid, "amy", "*", "all")
    with pytest.raises(ValueError):
        box.send(rid, "amy", "nobody", "x")
    with pytest.raises(ValueError):
        box.send(rid, "amy", "amy", "me")
    with pytest.raises(ValueError, match="only the lead"):
        box.broadcast(rid, "amy", "everyone")
    assert box.broadcast(rid, LEAD, "stop and commit") == 2
    assert [m.text for m in box.unread(rid, "bob")] == ["need T1 api", "stop and commit"]
    assert box.unread(rid, "bob") == []
    box.send(rid, "bob", LEAD, "done")
    assert box.count_unread(rid, LEAD) == 1
    assert [e.data["to"] for e in store.events(rid, kinds=["team_message"])] == [
        "bob",
        "*",
        "lead",
    ]


def test_delete_run_forgets_everything(store):
    rid = new_run(store)
    store.add_task(rid, title="a")
    store.add_member(rid, "dev", {})
    store.log_event(rid, "team_task")
    store.delete_run(rid)
    assert store.run(rid) is None and store.tasks(rid) == [] and store.events(rid) == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_team_board.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna_core.team'`

- [ ] **Step 3: Implement `team/settings.py`**

```python
"""Limits and knobs of a team run (``[team]`` in the app config)."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass

_POSITIVE_INTS = (
    "max_concurrent",
    "max_members",
    "member_steps",
    "lead_steps",
    "max_attempts",
    "stall_wakes",
)
_POSITIVE_NUMBERS = ("rpm", "wall_clock", "verify_timeout")


@dataclass(frozen=True)
class TeamSettings:
    """Limits of one team run; defaults follow the spec (2.5)."""

    max_concurrent: int = 4
    max_members: int = 8
    member_steps: int = 30
    lead_steps: int = 60
    rpm: float = 30.0
    budget: float | None = None
    wall_clock: float = 7200.0
    verify_command: str = ""
    verify_timeout: float = 600.0
    max_attempts: int = 3
    stall_wakes: int = 3
    allow_sleep: bool = False
    models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject values a run cannot work with."""
        for name in _POSITIVE_INTS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"team {name} must be an integer >= 1, got {value!r}")
        for name in _POSITIVE_NUMBERS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"team {name} must be a number > 0, got {value!r}")
        budget = self.budget
        if budget is not None and (
            isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget < 0
        ):
            raise ValueError(f"team budget must be a number >= 0, got {budget!r}")

    def to_dict(self) -> dict:
        """JSON-friendly form (stored with the run)."""
        data = dataclasses.asdict(self)
        data["models"] = list(self.models)
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> TeamSettings:
        """Inverse of :meth:`to_dict`; unknown keys are ignored."""
        known = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        if "models" in values:
            values["models"] = tuple(values["models"])
        return cls(**values)
```

- [ ] **Step 4: Implement `team/board.py`**

```python
"""The team board: runs, tasks, members and events of team runs in ``sessions.db``.

Lives in the same SQLite file as the session event log, on its own
connection (like :mod:`luna_core.assemblies`). Every write is one
``BEGIN IMMEDIATE`` transaction, so claiming a task is atomic across
threads and processes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

TASK_STATUSES = ("pending", "in_progress", "done", "failed", "blocked")
TASK_KINDS = ("code", "research")
RUN_STATUSES = ("planning", "awaiting_plan", "running", "finished")
OUTCOMES = ("done", "partial", "stopped", "stalled", "error")
MEMBER_STATUSES = ("idle", "working", "retired")
#: The lead's name on the board and in messages.
LEAD = "lead"

_DDL = """
CREATE TABLE IF NOT EXISTS team_runs(
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    workdir TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    stop_reason TEXT,
    session_id TEXT NOT NULL,
    branch TEXT,
    base TEXT,
    plan_json TEXT,
    settings_json TEXT NOT NULL,
    budget REAL,
    started REAL,
    finished REAL
);
CREATE TABLE IF NOT EXISTS team_tasks(
    run_id TEXT NOT NULL,
    id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    deps_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    assignee TEXT,
    note TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    branch TEXT,
    worktree TEXT,
    merge_of TEXT,
    commits_json TEXT NOT NULL DEFAULT '[]',
    updated REAL NOT NULL,
    PRIMARY KEY(run_id, id)
);
CREATE TABLE IF NOT EXISTS team_members(
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL,
    steps INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    busy REAL NOT NULL DEFAULT 0,
    hired REAL NOT NULL,
    PRIMARY KEY(run_id, name)
);
CREATE TABLE IF NOT EXISTS team_messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    text TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS team_messages_inbox ON team_messages(run_id, recipient, read);
CREATE TABLE IF NOT EXISTS team_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    member TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS team_events_run ON team_events(run_id, id);
"""

_RUN_COLS = (
    "id, created, updated, workdir, task, status, outcome, stop_reason, session_id, "
    "branch, base, plan_json, settings_json, budget, started, finished"
)
_TASK_COLS = (
    "run_id, id, seq, title, detail, kind, deps_json, status, assignee, note, attempts, "
    "branch, worktree, merge_of, commits_json, updated"
)
_MEMBER_COLS = "run_id, name, spec_json, status, steps, cost, busy, hired"
_EVENT_COLS = "id, run_id, ts, kind, member, data_json"
_RUN_FIELDS = frozenset(
    {"status", "outcome", "stop_reason", "branch", "base", "plan", "budget", "started", "finished"}
)
_TASK_FIELDS = frozenset(
    {"title", "detail", "status", "assignee", "note", "attempts", "branch", "worktree", "deps",
     "commits"}
)  # fmt: skip
_TASK_ID = re.compile(r"^T(\d+)$")


@dataclass(frozen=True)
class TeamRun:
    """One team run."""

    id: str
    created: float
    updated: float
    workdir: str
    task: str
    status: str
    outcome: str | None
    stop_reason: str | None
    session_id: str
    branch: str | None
    base: str | None
    plan: dict | None
    settings: dict
    budget: float | None
    started: float | None
    finished: float | None


@dataclass(frozen=True)
class TeamTask:
    """One task on the board."""

    run_id: str
    id: str
    seq: int
    title: str
    detail: str
    kind: str
    deps: tuple[str, ...]
    status: str
    assignee: str | None
    note: str
    attempts: int
    branch: str | None
    worktree: str | None
    merge_of: str | None
    commits: tuple[str, ...]
    updated: float


@dataclass(frozen=True)
class TeamMember:
    """A member (or the lead) of a run with its running totals."""

    run_id: str
    name: str
    spec: dict
    status: str
    steps: int
    cost: float
    busy: float
    hired: float


@dataclass(frozen=True)
class TeamEvent:
    """Something that happened in a run (also the protocol's ``team_*`` events)."""

    id: int
    run_id: str
    ts: float
    kind: str
    member: str
    data: dict


def _run(row: tuple) -> TeamRun:
    (rid, created, updated, workdir, task, status, outcome, stop_reason, session_id, branch,
     base, plan_json, settings_json, budget, started, finished) = row  # fmt: skip
    return TeamRun(
        rid,
        created,
        updated,
        workdir,
        task,
        status,
        outcome,
        stop_reason,
        session_id,
        branch,
        base,
        json.loads(plan_json) if plan_json else None,
        json.loads(settings_json),
        budget,
        started,
        finished,
    )


def _task(row: tuple) -> TeamTask:
    (run_id, tid, seq, title, detail, kind, deps_json, status, assignee, note, attempts, branch,
     worktree, merge_of, commits_json, updated) = row  # fmt: skip
    return TeamTask(
        run_id,
        tid,
        seq,
        title,
        detail,
        kind,
        tuple(json.loads(deps_json)),
        status,
        assignee,
        note,
        attempts,
        branch,
        worktree,
        merge_of,
        tuple(json.loads(commits_json)),
        updated,
    )


def _member(row: tuple) -> TeamMember:
    run_id, name, spec_json, status, steps, cost, busy, hired = row
    return TeamMember(run_id, name, json.loads(spec_json), status, steps, cost, busy, hired)


def _event(row: tuple) -> TeamEvent:
    eid, run_id, ts, kind, member, data_json = row
    return TeamEvent(eid, run_id, ts, kind, member, json.loads(data_json))


def _ready(task: TeamTask, by_id: dict[str, TeamTask]) -> bool:
    return task.status == "pending" and all(
        dep in by_id and by_id[dep].status == "done" for dep in task.deps
    )


class TeamStore:
    """Team runs over ``path`` (usually ``sessions.db``)."""

    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_DDL)
        self._lock = threading.Lock()
        self._listeners: list[Callable[[TeamEvent], None]] = []

    @contextlib.contextmanager
    def _write(self):
        """Process lock plus one ``BEGIN IMMEDIATE`` transaction (atomic across processes)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _read(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # --- runs -------------------------------------------------------------

    def create_run(
        self,
        *,
        workdir: str,
        task: str,
        session_id: str,
        settings: dict,
        budget: float | None = None,
    ) -> str:
        """Create a run in ``planning`` and return its id."""
        run_id = uuid.uuid4().hex[:8]
        now = time.time()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO team_runs(id, created, updated, workdir, task, status, session_id, "
                "settings_json, budget) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, now, now, workdir, task, "planning", session_id, json.dumps(settings),
                 budget),
            )  # fmt: skip
        return run_id

    def run(self, run_id: str) -> TeamRun | None:
        """Return one run or None."""
        rows = self._read(f"SELECT {_RUN_COLS} FROM team_runs WHERE id = ?", (run_id,))
        return _run(rows[0]) if rows else None

    def runs(
        self, *, workdir: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[TeamRun]:
        """Runs, newest first, optionally of one directory and/or status."""
        where, args = [], []
        if workdir is not None:
            where.append("workdir = ?")
            args.append(workdir)
        if status is not None:
            where.append("status = ?")
            args.append(status)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        rows = self._read(
            f"SELECT {_RUN_COLS} FROM team_runs {clause}ORDER BY created DESC LIMIT ?",
            (*args, limit),
        )
        return [_run(r) for r in rows]

    def update_run(self, run_id: str, **values: object) -> None:
        """Change fields of a run (``plan`` is a dict)."""
        unknown = set(values) - _RUN_FIELDS
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")
        if "status" in values and values["status"] not in RUN_STATUSES:
            raise ValueError(f"run status must be one of {RUN_STATUSES}, got {values['status']!r}")
        if values.get("outcome") is not None and values["outcome"] not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {values['outcome']!r}")
        cols: dict[str, object] = {}
        for key, value in values.items():
            if key == "plan":
                cols["plan_json"] = None if value is None else json.dumps(value, ensure_ascii=False)
            else:
                cols[key] = value
        cols["updated"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self._write() as conn:
            cur = conn.execute(f"UPDATE team_runs SET {sets} WHERE id = ?", (*cols.values(), run_id))
            if cur.rowcount == 0:
                raise KeyError(f"no team run {run_id}")

    def delete_run(self, run_id: str) -> None:
        """Forget a run and everything recorded for it."""
        with self._write() as conn:
            conn.execute("DELETE FROM team_runs WHERE id = ?", (run_id,))
            for table in ("team_tasks", "team_members", "team_messages", "team_events"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))

    # --- tasks ------------------------------------------------------------

    def _tasks_in(self, conn: sqlite3.Connection, run_id: str) -> list[TeamTask]:
        rows = conn.execute(
            f"SELECT {_TASK_COLS} FROM team_tasks WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [_task(r) for r in rows]

    def add_task(
        self,
        run_id: str,
        *,
        title: str,
        kind: str = "code",
        detail: str = "",
        deps: Iterable[str] = (),
        assignee: str | None = None,
        merge_of: str | None = None,
        task_id: str | None = None,
    ) -> TeamTask:
        """Add a pending task; its id is ``task_id`` or the next free ``T<n>``."""
        if kind not in TASK_KINDS:
            raise ValueError(f"task kind must be one of {TASK_KINDS}, got {kind!r}")
        if not title.strip():
            raise ValueError("a task needs a title")
        wanted = tuple(dict.fromkeys(deps))
        with self._write() as conn:
            rows = conn.execute(
                "SELECT id, seq FROM team_tasks WHERE run_id = ?", (run_id,)
            ).fetchall()
            ids = {r[0] for r in rows}
            missing = sorted(set(wanted) - ids)
            if missing:
                raise ValueError(f"unknown dependencies: {', '.join(missing)}")
            if task_id is None:
                numbers = [int(m.group(1)) for r in rows if (m := _TASK_ID.match(r[0]))]
                task_id = f"T{max(numbers, default=0) + 1}"
            elif task_id in ids:
                raise ValueError(f"task {task_id} already exists")
            seq = max((r[1] for r in rows), default=0) + 1
            conn.execute(
                "INSERT INTO team_tasks(run_id, id, seq, title, detail, kind, deps_json, status, "
                "assignee, merge_of, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, seq, title.strip(), detail, kind, json.dumps(list(wanted)),
                 "pending", assignee, merge_of, time.time()),
            )  # fmt: skip
        return self.task(run_id, task_id)

    def tasks(self, run_id: str) -> list[TeamTask]:
        """Every task of a run in board order."""
        with self._lock:
            return self._tasks_in(self._conn, run_id)

    def task(self, run_id: str, task_id: str) -> TeamTask | None:
        """One task or None."""
        rows = self._read(
            f"SELECT {_TASK_COLS} FROM team_tasks WHERE run_id = ? AND id = ?", (run_id, task_id)
        )
        return _task(rows[0]) if rows else None

    def ready(self, run_id: str) -> list[TeamTask]:
        """Pending tasks whose dependencies are all done."""
        tasks = self.tasks(run_id)
        by_id = {t.id: t for t in tasks}
        return [t for t in tasks if _ready(t, by_id)]

    @staticmethod
    def _take(conn: sqlite3.Connection, task: TeamTask, member: str) -> TeamTask:
        now = time.time()
        conn.execute(
            "UPDATE team_tasks SET status = 'in_progress', assignee = ?, attempts = attempts + 1, "
            "updated = ? WHERE run_id = ? AND id = ?",
            (member, now, task.run_id, task.id),
        )
        return dataclasses.replace(
            task, status="in_progress", assignee=member, attempts=task.attempts + 1, updated=now
        )

    def claim(self, run_id: str, task_id: str, member: str) -> bool:
        """Atomically take one ready task for ``member``; False when it is not available."""
        with self._write() as conn:
            by_id = {t.id: t for t in self._tasks_in(conn, run_id)}
            task = by_id.get(task_id)
            if task is None or not _ready(task, by_id) or task.assignee not in (None, member):
                return False
            self._take(conn, task, member)
            return True

    def claim_next(self, run_id: str, member: str) -> TeamTask | None:
        """Atomically take the next ready task for ``member`` (its own tasks first)."""
        with self._write() as conn:
            tasks = self._tasks_in(conn, run_id)
            by_id = {t.id: t for t in tasks}
            mine = [t for t in tasks if _ready(t, by_id) and t.assignee in (None, member)]
            if not mine:
                return None
            mine.sort(key=lambda t: (t.assignee != member, t.seq))
            return self._take(conn, mine[0], member)

    def update_task(self, run_id: str, task_id: str, **values: object) -> TeamTask:
        """Change fields of a task and return it (``deps`` / ``commits`` are lists)."""
        unknown = set(values) - _TASK_FIELDS
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        if "status" in values and values["status"] not in TASK_STATUSES:
            raise ValueError(
                f"task status must be one of {TASK_STATUSES}, got {values['status']!r}"
            )
        cols: dict[str, object] = {}
        for key, value in values.items():
            if key in ("deps", "commits"):
                cols[f"{key}_json"] = json.dumps(list(value))  # type: ignore[arg-type]
            else:
                cols[key] = value
        cols["updated"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self._write() as conn:
            cur = conn.execute(
                f"UPDATE team_tasks SET {sets} WHERE run_id = ? AND id = ?",
                (*cols.values(), run_id, task_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no task {task_id} in team {run_id}")
            row = conn.execute(
                f"SELECT {_TASK_COLS} FROM team_tasks WHERE run_id = ? AND id = ?",
                (run_id, task_id),
            ).fetchone()
        return _task(row)

    def remove_task(self, run_id: str, task_id: str, *, replace_with: Iterable[str] = ()) -> None:
        """Delete a task; tasks that depended on it depend on ``replace_with`` instead."""
        extra = list(dict.fromkeys(replace_with))
        with self._write() as conn:
            cur = conn.execute(
                "DELETE FROM team_tasks WHERE run_id = ? AND id = ?", (run_id, task_id)
            )
            if cur.rowcount == 0:
                raise KeyError(f"no task {task_id} in team {run_id}")
            rows = conn.execute(
                "SELECT id, deps_json FROM team_tasks WHERE run_id = ?", (run_id,)
            ).fetchall()
            for tid, deps_json in rows:
                deps = json.loads(deps_json)
                if task_id in deps:
                    new = [d for d in deps if d != task_id] + [e for e in extra if e not in deps]
                    conn.execute(
                        "UPDATE team_tasks SET deps_json = ? WHERE run_id = ? AND id = ?",
                        (json.dumps(new), run_id, tid),
                    )

    # --- members ----------------------------------------------------------

    def add_member(self, run_id: str, name: str, spec: dict) -> TeamMember:
        """Hire a member (idle); a second member with the same name is a ValueError."""
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT INTO team_members(run_id, name, spec_json, status, hired) "
                    "VALUES (?,?,?,?,?)",
                    (run_id, name, json.dumps(spec, ensure_ascii=False), "idle", time.time()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"member {name!r} is already on team {run_id}") from exc
        return self.member(run_id, name)

    def members(self, run_id: str, *, include_retired: bool = True) -> list[TeamMember]:
        """Members in hiring order."""
        rows = self._read(
            f"SELECT {_MEMBER_COLS} FROM team_members WHERE run_id = ? ORDER BY rowid", (run_id,)
        )
        members = [_member(r) for r in rows]
        return [m for m in members if include_retired or m.status != "retired"]

    def member(self, run_id: str, name: str) -> TeamMember | None:
        """One member or None."""
        rows = self._read(
            f"SELECT {_MEMBER_COLS} FROM team_members WHERE run_id = ? AND name = ?",
            (run_id, name),
        )
        return _member(rows[0]) if rows else None

    def update_member(
        self,
        run_id: str,
        name: str,
        *,
        status: str | None = None,
        add_steps: int = 0,
        add_cost: float = 0.0,
        add_busy: float = 0.0,
    ) -> None:
        """Set a member's status and add to its totals."""
        if status is not None and status not in MEMBER_STATUSES:
            raise ValueError(f"member status must be one of {MEMBER_STATUSES}, got {status!r}")
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE team_members SET status = COALESCE(?, status), steps = steps + ?, "
                "cost = cost + ?, busy = busy + ? WHERE run_id = ? AND name = ?",
                (status, add_steps, float(add_cost), float(add_busy), run_id, name),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no member {name} in team {run_id}")

    # --- events -----------------------------------------------------------

    def log_event(self, run_id: str, kind: str, member: str = "", **data: object) -> TeamEvent:
        """Record an event and tell the subscribers."""
        ts = time.time()
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO team_events(run_id, ts, kind, member, data_json) VALUES (?,?,?,?,?)",
                (run_id, ts, kind, member, json.dumps(data, ensure_ascii=False, default=str)),
            )
            event = TeamEvent(cur.lastrowid, run_id, ts, kind, member, dict(data))
        for fn in list(self._listeners):
            with contextlib.suppress(Exception):
                fn(event)
        return event

    def events(
        self, run_id: str, *, after: int = 0, kinds: Iterable[str] | None = None
    ) -> list[TeamEvent]:
        """Events of a run after id ``after``, oldest first, optionally of some kinds."""
        rows = self._read(
            f"SELECT {_EVENT_COLS} FROM team_events WHERE run_id = ? AND id > ? ORDER BY id",
            (run_id, after),
        )
        wanted = None if kinds is None else set(kinds)
        return [_event(r) for r in rows if wanted is None or r[3] in wanted]

    def subscribe(self, fn: Callable[[TeamEvent], None]) -> Callable[[], None]:
        """Call ``fn`` with every new event of this store; returns the unsubscribe function."""
        self._listeners.append(fn)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(fn)

        return unsubscribe

    def fingerprint(self, run_id: str) -> str:
        """A hash of the board state (tasks and members, not messages) for the stall detector."""
        tasks = [(t.id, t.status, t.assignee, t.attempts, list(t.deps)) for t in self.tasks(run_id)]
        members = [(m.name, m.status) for m in self.members(run_id)]
        return hashlib.sha256(json.dumps([tasks, members]).encode()).hexdigest()

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()
```

- [ ] **Step 5: Implement `team/messages.py`**

```python
"""Messages between team members: direct only; everybody at once only from the lead."""

from __future__ import annotations

import time
from dataclasses import dataclass

from luna_core.team.board import LEAD, TeamStore


@dataclass(frozen=True)
class TeamMessage:
    """One delivered message."""

    id: int
    run_id: str
    ts: float
    sender: str
    recipient: str
    text: str


class Mailbox:
    """Messages of team runs, stored next to the board."""

    def __init__(self, store: TeamStore) -> None:
        self.store = store

    def _recipients(self, run_id: str) -> set[str]:
        return {m.name for m in self.store.members(run_id, include_retired=False)} | {LEAD}

    def _insert(self, run_id: str, sender: str, recipient: str, text: str) -> TeamMessage:
        ts = time.time()
        with self.store._write() as conn:
            cur = conn.execute(
                "INSERT INTO team_messages(run_id, ts, sender, recipient, text) VALUES (?,?,?,?,?)",
                (run_id, ts, sender, recipient, text),
            )
        return TeamMessage(cur.lastrowid, run_id, ts, sender, recipient, text)

    def send(self, run_id: str, sender: str, recipient: str, text: str) -> TeamMessage:
        """Send ``text`` to one member or the lead."""
        text = text.strip()
        if not text:
            raise ValueError("a message needs text")
        if recipient == sender:
            raise ValueError("you cannot message yourself")
        if recipient not in self._recipients(run_id):
            raise ValueError(
                f"no member {recipient!r}; send to one member or 'lead' "
                "(only the lead may message everybody)"
            )
        message = self._insert(run_id, sender, recipient, text)
        self.store.log_event(run_id, "team_message", sender, to=recipient, text=text[:500])
        return message

    def broadcast(self, run_id: str, sender: str, text: str) -> int:
        """Send ``text`` to every active member; only the lead may. Returns how many got it."""
        if sender != LEAD:
            raise ValueError("only the lead may broadcast")
        text = text.strip()
        if not text:
            raise ValueError("a message needs text")
        names = [
            m.name for m in self.store.members(run_id, include_retired=False) if m.name != LEAD
        ]
        for name in names:
            self._insert(run_id, LEAD, name, text)
        self.store.log_event(run_id, "team_message", LEAD, to="*", text=text[:500])
        return len(names)

    def unread(self, run_id: str, recipient: str, *, mark: bool = True) -> list[TeamMessage]:
        """Unread messages for ``recipient``, oldest first (marked read unless ``mark`` is off)."""
        with self.store._write() as conn:
            rows = conn.execute(
                "SELECT id, run_id, ts, sender, recipient, text FROM team_messages "
                "WHERE run_id = ? AND recipient = ? AND read = 0 ORDER BY id",
                (run_id, recipient),
            ).fetchall()
            if mark and rows:
                marks = ",".join("?" * len(rows))
                conn.execute(
                    f"UPDATE team_messages SET read = 1 WHERE id IN ({marks})", [r[0] for r in rows]
                )
        return [TeamMessage(*r) for r in rows]

    def count_unread(self, run_id: str, recipient: str) -> int:
        """How many unread messages ``recipient`` has."""
        rows = self.store._read(
            "SELECT COUNT(*) FROM team_messages WHERE run_id = ? AND recipient = ? AND read = 0",
            (run_id, recipient),
        )
        return int(rows[0][0])
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest packages/luna-core/tests/test_team_board.py -q && uv run ruff check packages/luna-core/src/luna_core/team && uv run ruff format --check packages/luna-core/src/luna_core/team`
Expected: PASS. If ruff format wants to reflow lines marked `# fmt: skip`, leave them. Fix any other formatting it reports by running `uv run ruff format`.

- [ ] **Step 7: Commit**

```bash
git add packages/luna-core/src/luna_core/team packages/luna-core/tests/test_team_board.py
git commit -m "feat(team): board store, settings and mailbox in sessions.db"
```

---
### Task 4: The plan schema and the read-only planner

**Files:**
- Create: `packages/luna-core/src/luna_core/team/plan.py`
- Test: `packages/luna-core/tests/test_team_plan.py`

**Interfaces:**
- Consumes: `LEAD`, `TASK_KINDS` from `luna_core.team.board`; `check_rule_safe`, `UnsafeRule`, `Policy` from `luna_core.policy`; `Agent`; `READ_ONLY` from `luna_core.subagents`.
- Produces:
  - `PlanError(LunaCoreError)`;
  - `PlanTask(id, title, kind="code", deps=(), detail="", member=None)`;
  - `PlanMember(name, profile=None, prompt=None, tools=None, skills=(), model=None)` with `.assembly() -> dict` (the `resolve_spec` args), `.to_dict()`, `PlanMember.from_dict(data)`;
  - `TeamPlan(goal, criteria, tasks, members, allow=(), estimate={}, notes="")` with `.to_dict()`, `TeamPlan.from_dict(data)`, `.ordered_tasks() -> list[PlanTask]` (dependencies first);
  - `parse_member(item, profiles=None) -> PlanMember`;
  - `parse_plan(data, *, max_members, profiles=None) -> TeamPlan`;
  - `PLAN_SCHEMA`;
  - `Planner(provider, model, *, workspace, tools, max_members, profiles, max_steps, check_member=None, model_overrides=None, sandbox=None)` with `async plan(session, task, *, feedback=None, previous=None) -> TeamPlan`. Its system prompt contains the marker `Team lead`.

- [ ] **Step 1: Write the failing tests**

```python
"""The team plan: parsing, checks and the read-only planner."""

import copy

import pytest
from luna_core.errors import LunaCoreError
from luna_core.messages import Text, ToolResult
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session
from luna_core.team.plan import Planner, PlanError, TeamPlan, parse_plan
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_team_plan.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna_core.team.plan'`

- [ ] **Step 3: Implement `team/plan.py`**

```python
"""The team plan: its schema, its checks and the read-only lead that writes it."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field

from luna_core.context import build_system_prompt
from luna_core.errors import LunaCoreError
from luna_core.events import TurnError
from luna_core.loop import Agent
from luna_core.policy import Policy, UnsafeRule, check_rule_safe
from luna_core.providers.base import Provider
from luna_core.sandbox import Sandbox
from luna_core.session import Session
from luna_core.subagents import READ_ONLY
from luna_core.team.board import LEAD, TASK_KINDS
from luna_core.tools.base import Tool, ToolContext, ToolOutput
from luna_core.workspace import Workspace

_TASK_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
_MEMBER = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
_ESTIMATE_KEYS = ("minutes", "requests", "cost")

LEAD_PLAN_PROMPT = """\
You are the Team lead of a Luna team run. A human gave the team one task and \
approves your plan once; after that the team works without asking anybody.

First explore the repository read-only (ls, glob, grep, read_file) just enough to \
plan. Then call submit_plan once with:
- goal and done criteria;
- tasks T1…Tn: small and independently mergeable, with deps and kind "code" (changes \
files in its own git worktree) or "research" (read-only, ends in a note);
- members: for each a profile name or an assembly (prompt, tools, skills, model), and \
the tasks they own via the task's "member";
- allow: the permission profile as rules like "execute:uv run pytest*". Anything not \
allowed is refused during the run, so list every command the members need;
- estimate: minutes, model requests and cost.
If submit_plan returns an error, fix the plan and call it again. Then end your turn."""

SUBMIT_DESCRIPTION = "Submit the team plan (goal, criteria, tasks, members, allow, estimate)."

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "criteria": {"type": "array", "items": {"type": "string"}},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": list(TASK_KINDS)},
                    "deps": {"type": "array", "items": {"type": "string"}},
                    "detail": {"type": "string"},
                    "member": {"type": "string"},
                },
                "required": ["id", "title", "kind"],
            },
        },
        "members": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "profile": {"type": "string"},
                    "prompt": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        "allow": {"type": "array", "items": {"type": "string"}},
        "estimate": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number"},
                "requests": {"type": "number"},
                "cost": {"type": "number"},
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["goal", "criteria", "tasks", "members"],
}


class PlanError(LunaCoreError):
    """A plan is invalid, or the lead did not produce one."""


@dataclass(frozen=True)
class PlanTask:
    """One task of the plan."""

    id: str
    title: str
    kind: str = "code"
    deps: tuple[str, ...] = ()
    detail: str = ""
    member: str | None = None

    def to_dict(self) -> dict:
        """JSON-friendly form."""
        data = {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "deps": list(self.deps),
            "detail": self.detail,
        }
        if self.member:
            data["member"] = self.member
        return data


@dataclass(frozen=True)
class PlanMember:
    """One member: a profile name and/or a dynamic assembly (spec part 1)."""

    name: str
    profile: str | None = None
    prompt: str | None = None
    tools: tuple[str, ...] | None = None
    skills: tuple[str, ...] = ()
    model: str | None = None

    def assembly(self) -> dict:
        """The ``resolve_spec`` arguments this member asks for."""
        out: dict = {}
        if self.prompt:
            out["prompt"] = self.prompt
        if self.tools is not None:
            out["tools"] = list(self.tools)
        if self.skills:
            out["skills"] = list(self.skills)
        if self.model:
            out["model"] = self.model
        return out

    def to_dict(self) -> dict:
        """JSON-friendly form (stored as the member's spec on the board)."""
        return {"name": self.name, **({"profile": self.profile} if self.profile else {}),
                **self.assembly()}  # fmt: skip

    @classmethod
    def from_dict(cls, data: Mapping) -> PlanMember:
        """Inverse of :meth:`to_dict`."""
        return parse_member(data)


@dataclass(frozen=True)
class TeamPlan:
    """What the lead proposes and the human accepts once."""

    goal: str
    criteria: tuple[str, ...]
    tasks: tuple[PlanTask, ...]
    members: tuple[PlanMember, ...]
    allow: tuple[str, ...] = ()
    estimate: dict = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        """JSON-friendly form (stored with the run, shown to the human)."""
        return {
            "goal": self.goal,
            "criteria": list(self.criteria),
            "tasks": [t.to_dict() for t in self.tasks],
            "members": [m.to_dict() for m in self.members],
            "allow": list(self.allow),
            "estimate": dict(self.estimate),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> TeamPlan:
        """Inverse of :meth:`to_dict` (checked again, without a member limit)."""
        return parse_plan(data, max_members=max(1, len(data.get("members") or ())))

    def ordered_tasks(self) -> list[PlanTask]:
        """The tasks with every dependency before its dependents (stable otherwise)."""
        done: set[str] = set()
        out: list[PlanTask] = []
        left = list(self.tasks)
        while left:
            for task in left:
                if set(task.deps) <= done:
                    out.append(task)
                    done.add(task.id)
                    left.remove(task)
                    break
            else:
                raise PlanError("task dependencies form a cycle")
        return out


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{what} must be a non-empty string")
    return value.strip()


def _strings(value: object, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PlanError(f"{what} must be a list of non-empty strings")
    return tuple(v.strip() for v in value)


def parse_member(item: object, profiles: Collection[str] | None = None) -> PlanMember:
    """Check one member entry; ``profiles`` (when given) are the known profile names."""
    if not isinstance(item, Mapping):
        raise PlanError("each member must be an object")
    name = _text(item.get("name"), "member name")
    if name == LEAD or not _MEMBER.match(name):
        raise PlanError(
            f"member name {name!r}: use lowercase letters, digits and dashes, "
            "starting with a letter (not 'lead')"
        )
    profile = item.get("profile") or None
    if profile is not None:
        profile = _text(profile, f"member {name} profile")
        if profiles is not None and profile not in profiles:
            known = ", ".join(sorted(profiles)) or "none"
            raise PlanError(f"member {name}: unknown profile {profile!r}; available: {known}")
    prompt = item.get("prompt") or None
    if prompt is not None:
        prompt = _text(prompt, f"member {name} prompt")
    if profile is None and prompt is None:
        raise PlanError(f"member {name}: give a profile or a prompt")
    tools = item.get("tools")
    tools = None if tools is None else _strings(tools, f"member {name} tools")
    if tools == ():
        raise PlanError(f"member {name}: tools must name at least one tool")
    model = item.get("model") or None
    if model is not None:
        model = _text(model, f"member {name} model")
    skills = _strings(item.get("skills"), f"member {name} skills")
    return PlanMember(name, profile, prompt, tools, skills, model)


def _parse_task(item: object, members: Iterable[str]) -> PlanTask:
    if not isinstance(item, Mapping):
        raise PlanError("each task must be an object")
    tid = _text(item.get("id"), "task id")
    if not _TASK_ID.match(tid):
        raise PlanError(f"task id {tid!r}: use a short name like T1")
    title = _text(item.get("title"), f"task {tid} title")
    kind = item.get("kind") or "code"
    if kind not in TASK_KINDS:
        raise PlanError(f"task {tid}: kind must be one of {', '.join(TASK_KINDS)}, got {kind!r}")
    deps = _strings(item.get("deps"), f"task {tid} deps")
    detail = item.get("detail") or ""
    if not isinstance(detail, str):
        raise PlanError(f"task {tid}: detail must be a string")
    member = item.get("member") or None
    if member is not None and member not in set(members):
        raise PlanError(f"task {tid}: unknown member {member!r}")
    return PlanTask(tid, title, kind, deps, detail.strip(), member)


def _check_acyclic(tasks: Iterable[PlanTask]) -> None:
    deps = {t.id: set(t.deps) for t in tasks}
    done: set[str] = set()
    while deps:
        free = [k for k, v in deps.items() if v <= done]
        if not free:
            raise PlanError(f"task dependencies form a cycle: {', '.join(sorted(deps))}")
        for key in free:
            done.add(key)
            del deps[key]


def parse_plan(
    data: object, *, max_members: int, profiles: Collection[str] | None = None
) -> TeamPlan:
    """Check a plan from the lead (or from storage) and return it."""
    if not isinstance(data, Mapping):
        raise PlanError("the plan must be an object")
    goal = _text(data.get("goal"), "goal")
    criteria = _strings(data.get("criteria"), "criteria")
    if not criteria:
        raise PlanError("criteria must list at least one done criterion")
    raw_members = data.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        raise PlanError("members must list at least one member")
    if len(raw_members) > max_members:
        raise PlanError(f"at most {max_members} members per run, got {len(raw_members)}")
    members = tuple(parse_member(item, profiles) for item in raw_members)
    names = [m.name for m in members]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise PlanError(f"duplicate member names: {', '.join(dupes)}")
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanError("tasks must list at least one task")
    tasks = tuple(_parse_task(item, names) for item in raw_tasks)
    ids = [t.id for t in tasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise PlanError(f"duplicate task ids: {', '.join(dupes)}")
    for task in tasks:
        unknown = sorted(set(task.deps) - set(ids))
        if unknown:
            raise PlanError(f"task {task.id}: unknown dependencies {', '.join(unknown)}")
        if task.id in task.deps:
            raise PlanError(f"task {task.id} depends on itself")
    _check_acyclic(tasks)
    allow = _strings(data.get("allow"), "allow")
    for rule in allow:
        tool, sep, pattern = rule.partition(":")
        if not sep or not tool or not pattern:
            raise PlanError(f"permission rule {rule!r}: write <tool>:<pattern>")
        try:
            check_rule_safe(rule)
        except UnsafeRule as exc:
            raise PlanError(str(exc)) from exc
    estimate = data.get("estimate") or {}
    if not isinstance(estimate, Mapping):
        raise PlanError("estimate must be an object")
    clean: dict = {}
    for key in _ESTIMATE_KEYS:
        value = estimate.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise PlanError(f"estimate {key} must be a number >= 0")
        clean[key] = value
    notes = data.get("notes") or ""
    if not isinstance(notes, str):
        raise PlanError("notes must be a string")
    return TeamPlan(goal, criteria, tasks, members, allow, clean, notes.strip())


class Planner:
    """Runs the lead read-only until it submits a valid plan."""

    def __init__(
        self,
        provider: Provider,
        model: str,
        *,
        workspace: Workspace,
        tools: Iterable[Tool],
        max_members: int,
        profiles: Collection[str],
        max_steps: int,
        check_member: Callable[[PlanMember], object] | None = None,
        model_overrides: dict | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.workspace = workspace
        self.tools = list(tools)
        self.max_members = max_members
        self.profiles = profiles
        self.max_steps = max_steps
        self.check_member = check_member
        self.model_overrides = model_overrides
        self.sandbox = sandbox

    @staticmethod
    def _request(task: str, feedback: str | None, previous: TeamPlan | None) -> str:
        text = f"Plan this task for the team:\n\n{task}"
        if not feedback:
            return text
        before = (
            json.dumps(previous.to_dict(), ensure_ascii=False, indent=1) if previous else "(none)"
        )
        return (
            f"{text}\n\nYour previous plan:\n```json\n{before}\n```\n\n"
            f"The user wants changes:\n{feedback}\n\nSubmit the revised plan with submit_plan."
        )

    async def plan(
        self,
        session: Session,
        task: str,
        *,
        feedback: str | None = None,
        previous: TeamPlan | None = None,
    ) -> TeamPlan:
        """Run one lead turn in ``session`` and return the plan it submitted."""
        submitted: list[TeamPlan] = []

        async def submit(ctx: ToolContext, args: dict) -> ToolOutput:
            try:
                plan = parse_plan(args, max_members=self.max_members, profiles=self.profiles)
                if self.check_member is not None:
                    for member in plan.members:
                        self.check_member(member)
            except LunaCoreError as exc:
                return ToolOutput.error(f"plan rejected: {exc}")
            submitted.append(plan)
            return ToolOutput.text("plan recorded; end your turn now")

        tools = [t for t in self.tools if t.name in READ_ONLY]
        tools.append(Tool("submit_plan", SUBMIT_DESCRIPTION, PLAN_SCHEMA, submit, raw=True))
        agent = Agent(
            self.provider,
            self.model,
            tools=tools,
            system=build_system_prompt(LEAD_PLAN_PROMPT, root=self.workspace.root),
            policy=Policy(workspace=self.workspace, plan_mode=True),
            workspace=self.workspace,
            sandbox=self.sandbox,
            max_steps=self.max_steps,
            model_overrides=self.model_overrides,
            name=LEAD,
        )
        error = ""
        async for event in agent.run(session, self._request(task, feedback, previous)):
            if isinstance(event, TurnError):
                error = event.message
        if submitted:
            return submitted[-1]
        suffix = f": {error}" if error else ""
        raise PlanError(f"the lead finished without submitting a plan{suffix}")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/luna-core/tests/test_team_plan.py -q`
Expected: PASS. If the `c2` result is not an error because `execute` is somehow present, check the filter `t.name in READ_ONLY`: the planner must never get `execute`.

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/team/plan.py packages/luna-core/tests/test_team_plan.py
git commit -m "feat(team): plan schema with checks and a read-only planner"
```

---

### Task 5: The permission profile

**Files:**
- Create: `packages/luna-core/src/luna_core/team/permissions.py`
- Test: `packages/luna-core/tests/test_team_permissions.py`

**Interfaces:**
- Consumes: `Policy`, `Rules`, `Verdict`, `MUTATING_FILE_TOOLS`, `check_rule_safe`, `UnsafeRule` from `luna_core.policy`.
- Produces:
  - `DEFAULT_ALLOW`;
  - `default_allow(verify_command="") -> tuple[str, ...]`;
  - `Denied = Callable[[str, str, dict, str], None]`, called as `(member, tool, args, reason)` with reason `"profile"` or `"rule"`;
  - `TeamPolicy(allow, *, deny=(), workspace, member, on_denied, file_writes=False)`, a `Policy` that never asks: what the profile does not allow is denied and reported.

- [ ] **Step 1: Write the failing tests**

```python
"""The team permission profile: allow the plan's rules, deny the rest, deny rules win."""

from luna_core.team.permissions import DEFAULT_ALLOW, TeamPolicy, default_allow
from luna_core.tools import builtin_tools
from luna_core.workspace import Workspace

T = {t.name: t for t in builtin_tools()}


def make(tmp_path, allow=(), deny=(), file_writes=False):
    denied = []
    policy = TeamPolicy(
        allow,
        deny=deny,
        workspace=Workspace(tmp_path),
        member="dev",
        on_denied=lambda *a: denied.append(a),
        file_writes=file_writes,
    )
    return policy, denied


def test_commands_outside_the_profile_are_denied_and_recorded(tmp_path):
    policy, denied = make(tmp_path, allow=["execute:uv run pytest*"])
    assert policy.check(T["execute"], {"command": "uv run pytest -q"}).action == "allow"
    verdict = policy.check(T["execute"], {"command": "curl https://x"})
    assert verdict.action == "deny" and "permission profile" in verdict.message
    assert denied == [("dev", "execute", {"command": "curl https://x"}, "profile")]


def test_permissions_toml_deny_beats_the_profile(tmp_path):
    policy, denied = make(tmp_path, allow=["execute:git *"], deny=["execute:git push*"])
    assert policy.check(T["execute"], {"command": "git status"}).action == "allow"
    assert policy.check(T["execute"], {"command": "git push origin main"}).action == "deny"
    assert denied[-1][3] == "rule"


def test_file_writes_follow_the_task_kind(tmp_path):
    code, _ = make(tmp_path, file_writes=True)
    args = {"file_path": "a.py", "content": "x"}
    assert code.check(T["write_file"], args).action == "allow"
    research, denied = make(tmp_path)
    assert research.check(T["write_file"], args).action == "deny"
    assert denied[0][:2] == ("dev", "write_file")


def test_deny_rules_on_files_hold_even_for_code_tasks(tmp_path):
    policy, denied = make(tmp_path, deny=["write:.env"], file_writes=True)
    verdict = policy.check(T["write_file"], {"file_path": ".env", "content": "x"})
    assert verdict.action == "deny" and denied[0][3] == "rule"


def test_reads_are_allowed_and_never_reported(tmp_path):
    policy, denied = make(tmp_path)
    assert policy.check(T["read_file"], {"file_path": "a.py"}).action == "allow"
    assert denied == []


def test_child_keeps_the_profile(tmp_path):
    policy, denied = make(tmp_path, allow=["execute:git *"])
    child = policy.child(Workspace(tmp_path / "sub"))
    assert isinstance(child, TeamPolicy)
    assert child.check(T["execute"], {"command": "curl x"}).action == "deny"
    assert child.check(T["execute"], {"command": "git log"}).action == "allow"
    assert len(denied) == 1


def test_default_allow_adds_a_safe_verify_command():
    assert default_allow("") == DEFAULT_ALLOW
    assert default_allow("uv run  pytest -q") == (*DEFAULT_ALLOW, "execute:uv run pytest -q*")
    assert default_allow("bash -c 'make test'") == DEFAULT_ALLOW
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_team_permissions.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna_core.team.permissions'`

- [ ] **Step 3: Implement `team/permissions.py`**

```python
"""The permission profile of a team run: allow what the plan lists, deny the rest.

Nobody is asked during a run. A call the profile does not allow is denied
and reported through ``on_denied`` (it becomes a ``team_denied`` event and a
line in the report). Deny rules from ``permissions.toml`` always win.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from luna_core.policy import (
    MUTATING_FILE_TOOLS,
    Policy,
    Rules,
    UnsafeRule,
    Verdict,
    check_rule_safe,
)
from luna_core.tools.base import Tool, args_preview
from luna_core.workspace import Workspace

#: The profile a plan accepted unseen (``--yes``) starts from.
DEFAULT_ALLOW = (
    "execute:git status*",
    "execute:git diff*",
    "execute:git log*",
    "execute:git show*",
)
_RULE_DENY = "blocked by a Luna permission rule"

#: ``(member, tool, args, reason)`` with reason ``"profile"`` or ``"rule"``.
Denied = Callable[[str, str, dict, str], None]


def default_allow(verify_command: str = "") -> tuple[str, ...]:
    """Read-only git plus the verify command (when it is safe as an allow rule)."""
    command = " ".join(verify_command.split())
    if not command:
        return DEFAULT_ALLOW
    rule = f"execute:{command}*"
    try:
        check_rule_safe(rule)
    except UnsafeRule:
        return DEFAULT_ALLOW
    return (*DEFAULT_ALLOW, rule)


class TeamPolicy(Policy):
    """A policy that never asks a human.

    ``allow`` is the run's profile, ``deny`` the user's deny rules. With
    ``file_writes`` (a ``code`` task in its own worktree) file edits are
    allowed; everything else that would need approval is denied.
    """

    def __init__(
        self,
        allow: Iterable[str],
        *,
        deny: Iterable[str] = (),
        workspace: Workspace,
        member: str,
        on_denied: Denied,
        file_writes: bool = False,
    ) -> None:
        self.allow_rules = tuple(allow)
        super().__init__(Rules(allow=list(self.allow_rules), deny=list(deny)), workspace=workspace)
        self.member = member
        self.on_denied = on_denied
        self.file_writes = file_writes

    def check(self, tool: Tool, args: dict) -> Verdict:
        """Allow, or deny and report; never ask."""
        verdict = super().check(tool, args)
        if verdict.action == "deny":
            if verdict.message.startswith(_RULE_DENY):
                self.on_denied(self.member, tool.name, dict(args), "rule")
            return verdict
        if verdict.action != "ask":
            return verdict
        if self.file_writes and tool.name in MUTATING_FILE_TOOLS:
            return Verdict("allow")
        self.on_denied(self.member, tool.name, dict(args), "profile")
        return Verdict(
            "deny",
            f"{tool.name} ({args_preview(args, 80)}) is outside the team's permission profile "
            "and was not run; do the task without it or report it blocked with board_update",
        )

    def child(self, workspace: Workspace | None = None) -> TeamPolicy:
        """Same profile for anything the member starts (members do not delegate)."""
        return TeamPolicy(
            self.allow_rules,
            deny=self.rules.deny,
            workspace=workspace or self.workspace,
            member=self.member,
            on_denied=self.on_denied,
            file_writes=self.file_writes,
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/luna-core/tests/test_team_permissions.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/team/permissions.py packages/luna-core/tests/test_team_permissions.py
git commit -m "feat(team): permission profile that denies and reports instead of asking"
```

---

### Task 6: Git plumbing for a run

**Files:**
- Create: `packages/luna-core/src/luna_core/team/git.py`
- Test: `packages/luna-core/tests/test_team_git.py`

**Interfaces:**
- Consumes: `_git`, `_git_toplevel`, `_prune_worktrees`, `_worktree_lock`, `_IDENTITY`, `finish_worktree` from `luna_core.subagents`; `run_sync` from `luna_core.shell`.
- Produces:
  - `TeamGitError(LunaCoreError)`;
  - `MemberTree(path, branch, base)`;
  - `MergeResult(status, detail="", commit="", before="")`, where `status` is one of `"merged"`, `"empty"`, `"conflict"`, `"markers"`;
  - `team_branch(run_id) -> "luna/team/<id>"`, `task_branch(run_id, task_id) -> "luna/teamwork/<id>/<task>"`;
  - `TeamGit(root, run_id)` with `.branch` and `.integration` (Path) and these methods:
    - `start() -> base`, `ensure()`, `tip()`;
    - `member_tree(task_id, attempt, *, branch=None, merge=None) -> MemberTree`, `finish(tree, message) -> (diffstat, commits)`;
    - `markers(branch) -> list[str]`, `merge(branch, message) -> MergeResult`, `undo_merge(before)`, `verify(command, timeout) -> (ok, output)`;
    - `delete_branch(name)`, `has_branch(name) -> bool`, `diffstat(base) -> str`, `salvage(path, branch) -> diffstat`, `close()`.

- [ ] **Step 1: Write the failing tests**

```python
"""Git plumbing of a team run: team branch, member worktrees, serial merges."""

import subprocess

import pytest
from luna_core.team.git import TeamGit, TeamGitError


def git(root, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    return root


def test_start_creates_the_team_branch_and_leaves_the_user_tree_alone(repo):
    (repo / "a.txt").write_text("dirty\n")
    (repo / "mine.txt").write_text("untracked\n")
    tg = TeamGit(repo, "r1")
    base = tg.start()
    assert tg.tip() == base == git(repo, "rev-parse", "HEAD").strip()
    assert (tg.integration / "a.txt").read_text() == "one\n"
    assert not (tg.integration / "mine.txt").exists()
    assert (repo / "a.txt").read_text() == "dirty\n"
    assert git(repo, "branch", "--show-current").strip() == "main"
    with pytest.raises(TeamGitError, match="already exists"):
        TeamGit(repo, "r1").start()


def test_start_needs_a_commit(tmp_path):
    git(tmp_path, "init", "-q")
    with pytest.raises(TeamGitError, match="no commits"):
        TeamGit(tmp_path, "r1").start()


def test_member_work_merges_serially(repo):
    tg = TeamGit(repo, "r1")
    tg.start()
    t1 = tg.member_tree("T1", 1)
    assert t1.branch == "luna/teamwork/r1/T1"
    (t1.path / "b.txt").write_text("b\n")
    stat, commits = tg.finish(t1, "T1")
    assert "b.txt" in stat and len(commits) == 1 and not t1.path.exists()
    result = tg.merge(t1.branch, "merge T1")
    assert result.status == "merged" and result.commit == tg.tip()
    assert (tg.integration / "b.txt").exists()
    assert git(repo, "show", f"{tg.branch}:b.txt") == "b\n"
    assert tg.merge(t1.branch, "again").status == "empty"


def test_conflicting_work_is_reported_and_resolved_in_a_merge_tree(repo):
    tg = TeamGit(repo, "r1")
    tg.start()
    t1, t2 = tg.member_tree("T1", 1), tg.member_tree("T2", 1)
    (t1.path / "a.txt").write_text("from one\n")
    (t2.path / "a.txt").write_text("from two\n")
    tg.finish(t1, "T1")
    tg.finish(t2, "T2")
    assert tg.merge(t1.branch, "m1").status == "merged"
    conflict = tg.merge(t2.branch, "m2")
    assert conflict.status == "conflict" and "a.txt" in conflict.detail
    assert git(repo, "-C", str(tg.integration), "status", "--porcelain") == ""
    fix = tg.member_tree("T3", 1, merge=t2.branch)
    assert "<<<<<<<" in (fix.path / "a.txt").read_text()
    tg.finish(fix, "T3 unresolved")
    markers = tg.merge(fix.branch, "m3")
    assert markers.status == "markers" and "a.txt" in markers.detail
    retry = tg.member_tree("T3", 2, branch=fix.branch)
    (retry.path / "a.txt").write_text("from both\n")
    tg.finish(retry, "T3")
    assert tg.merge(retry.branch, "m3").status == "merged"
    assert git(repo, "show", f"{tg.branch}:a.txt") == "from both\n"


def test_verify_and_undo(repo):
    tg = TeamGit(repo, "r1")
    tg.start()
    t1 = tg.member_tree("T1", 1)
    (t1.path / "b.txt").write_text("b\n")
    tg.finish(t1, "T1")
    result = tg.merge(t1.branch, "m1")
    assert tg.verify("test -f b.txt", 30) == (True, "")
    ok, output = tg.verify("echo boom && exit 3", 30)
    assert not ok and "boom" in output
    tg.undo_merge(result.before)
    assert tg.tip() == result.before and not (tg.integration / "b.txt").exists()


def test_an_empty_attempt_leaves_no_branch(repo):
    tg = TeamGit(repo, "r1")
    tg.start()
    t1 = tg.member_tree("T1", 1)
    assert tg.finish(t1, "T1") == ("", [])
    assert not tg.has_branch(t1.branch)


def test_salvage_close_and_ensure(repo):
    tg = TeamGit(repo, "r1")
    base = tg.start()
    t1 = tg.member_tree("T1", 1)
    (t1.path / "c.txt").write_text("c\n")
    assert "c.txt" in tg.salvage(t1.path, t1.branch)
    assert not t1.path.exists() and tg.has_branch(t1.branch)
    tg.delete_branch(t1.branch)
    assert not tg.has_branch(t1.branch)
    tg.close()
    assert not tg.integration.exists() and tg.has_branch(tg.branch)
    tg.ensure()
    assert (tg.integration / "a.txt").exists()
    assert tg.diffstat(base) == ""
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_team_git.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna_core.team.git'`

- [ ] **Step 3: Implement `team/git.py`**

```python
"""Git plumbing for a team run: the team branch, member worktrees, serial merges.

The user's working tree and branch are never touched. The team branch
``luna/team/<id>`` starts at ``HEAD`` and lives checked out in its own
integration worktree; each task attempt gets a worktree on
``luna/teamwork/<id>/<task>`` from the team tip. The runner serialises
``merge`` calls.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from luna_core.errors import LunaCoreError
from luna_core.shell import run_sync
from luna_core.subagents import (
    _IDENTITY,
    _git,
    _git_toplevel,
    _prune_worktrees,
    _worktree_lock,
    finish_worktree,
)

_MARKERS = r"^(<{7}|>{7})( |$)"


class TeamGitError(LunaCoreError):
    """A git step of a team run failed."""


@dataclass(frozen=True)
class MemberTree:
    """The worktree of one task attempt."""

    path: Path
    branch: str
    base: str


@dataclass(frozen=True)
class MergeResult:
    """How merging a task branch into the team branch went."""

    status: str  # "merged" | "empty" | "conflict" | "markers"
    detail: str = ""
    commit: str = ""
    before: str = ""


def team_branch(run_id: str) -> str:
    """The run's result branch."""
    return f"luna/team/{run_id}"


def task_branch(run_id: str, task_id: str) -> str:
    """The branch of a task's attempts."""
    return f"luna/teamwork/{run_id}/{task_id}"


class TeamGit:
    """Git operations of one run in the repository at ``root``."""

    def __init__(self, root: str | Path, run_id: str) -> None:
        self.root = Path(root).resolve()
        self.run_id = run_id
        self.branch = team_branch(run_id)
        self.integration = self.root / ".luna" / "worktrees" / f"team-{run_id}"

    @staticmethod
    def _ok(proc: subprocess.CompletedProcess, what: str) -> str:
        if proc.returncode != 0:
            raise TeamGitError(f"{what}: {(proc.stderr or proc.stdout).strip()}")
        return proc.stdout.strip()

    def has_branch(self, name: str) -> bool:
        """Return True when the branch exists."""
        return _git(self.root, "rev-parse", "--verify", "-q", name).returncode == 0

    def start(self) -> str:
        """Create the team branch at ``HEAD`` and its integration worktree; return ``HEAD``."""
        top = _git_toplevel(self.root)
        if top is None or top != self.root:
            raise TeamGitError("a team run needs the project root to be a git repository root")
        with _worktree_lock(self.root):
            head = _git(self.root, "rev-parse", "--verify", "-q", "HEAD").stdout.strip()
            if not head:
                raise TeamGitError("the repository has no commits yet; commit once first")
            if self.has_branch(self.branch):
                raise TeamGitError(f"branch {self.branch} already exists")
            self._ok(_git(self.root, "branch", self.branch, head), "git branch")
            self._attach()
        return head

    def _attach(self) -> None:
        _prune_worktrees(self.root)
        if (self.integration / ".git").exists():
            return
        self.integration.parent.mkdir(parents=True, exist_ok=True)
        self._ok(
            _git(self.root, "worktree", "add", "-q", str(self.integration), self.branch),
            "git worktree add",
        )

    def ensure(self) -> None:
        """Re-attach the integration worktree (after a restart or a close)."""
        with _worktree_lock(self.root):
            self._attach()

    def tip(self) -> str:
        """The team branch's current commit."""
        return self._ok(_git(self.root, "rev-parse", self.branch), "git rev-parse")

    def member_tree(
        self, task_id: str, attempt: int, *, branch: str | None = None, merge: str | None = None
    ) -> MemberTree:
        """A worktree for one attempt: ``branch`` when it exists, else a new branch at the tip.

        With ``merge`` the new worktree starts merging that branch and leaves
        its conflicts in the files for the member to resolve.
        """
        path = self.root / ".luna" / "worktrees" / f"team-{self.run_id}-{task_id}-{attempt}"
        with _worktree_lock(self.root):
            _prune_worktrees(self.root)
            path.parent.mkdir(parents=True, exist_ok=True)
            if branch and self.has_branch(branch):
                self._ok(
                    _git(self.root, "worktree", "add", "-q", str(path), branch), "git worktree add"
                )
                base = self._ok(_git(self.root, "merge-base", branch, self.branch), "merge-base")
                return MemberTree(path, branch, base)
            name = task_branch(self.run_id, task_id)
            if self.has_branch(name):
                _git(self.root, "branch", "-D", name)
            tip = self.tip()
            self._ok(
                _git(self.root, "worktree", "add", "-q", "-b", name, str(path), tip),
                "git worktree add",
            )
        if merge:
            # Exits non-zero on conflicts: that is the point, the member resolves them.
            _git(
                path, "-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-commit", merge,
                env=_IDENTITY,
            )  # fmt: skip
        return MemberTree(path, name, tip)

    def finish(self, tree: MemberTree, message: str) -> tuple[str, list[str]]:
        """Commit the attempt and drop its worktree; return (diffstat, its commits)."""
        stat, _diff = finish_worktree(self.root, tree.path, tree.branch, tree.base, message)
        if not stat:
            # A retry that undid its own earlier work leaves a branch with nothing to merge.
            if self.has_branch(tree.branch):
                _git(self.root, "branch", "-D", tree.branch)
            return "", []
        listed = _git(self.root, "rev-list", "--reverse", f"{tree.base}..{tree.branch}")
        return stat, self._ok(listed, "git rev-list").split()

    def markers(self, branch: str) -> list[str]:
        """Files changed on ``branch`` that still hold conflict markers."""
        changed = _git(self.root, "diff", "--name-only", f"{self.branch}...{branch}").stdout
        names = [n for n in changed.splitlines() if n]
        if not names:
            return []
        found = _git(self.root, "grep", "-l", "-E", _MARKERS, branch, "--", *names).stdout
        return sorted({line.split(":", 1)[1] for line in found.splitlines() if ":" in line})

    def merge(self, branch: str, message: str) -> MergeResult:
        """Merge ``branch`` into the team branch (the caller serialises merges)."""
        marked = self.markers(branch)
        if marked:
            return MergeResult("markers", ", ".join(marked))
        before = self.tip()
        proc = _git(
            self.integration, "-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-verify",
            "-m", message, branch, env=_IDENTITY,
        )  # fmt: skip
        if proc.returncode != 0:
            _git(self.integration, "merge", "--abort")
            detail = (proc.stdout + proc.stderr).strip()[-2000:]
            return MergeResult("conflict", detail, before=before)
        after = self.tip()
        return MergeResult("merged" if after != before else "empty", commit=after, before=before)

    def undo_merge(self, before: str) -> None:
        """Move the team branch back to ``before`` (a merge that failed verification)."""
        self._ok(_git(self.integration, "reset", "--hard", "-q", before), "git reset")

    def verify(self, command: str, timeout: float) -> tuple[bool, str]:
        """Run the verify command on the team branch; return (passed, output tail)."""
        try:
            proc = run_sync(command, cwd=self.integration, timeout=timeout)
        except OSError as exc:
            return False, str(exc)
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output[-4000:]

    def delete_branch(self, name: str) -> None:
        """Delete a task branch (merged or abandoned)."""
        _git(self.root, "branch", "-D", name)

    def diffstat(self, base: str) -> str:
        """``git diff --stat`` of the whole run."""
        return _git(self.root, "diff", "--stat", base, self.branch).stdout.strip()

    def salvage(self, path: Path, branch: str) -> str:
        """Commit and drop a worktree left by an interrupted attempt; return its diffstat."""
        if not path.exists():
            _git(self.root, "worktree", "prune")
            return ""
        base = self._ok(_git(self.root, "merge-base", branch, self.branch), "git merge-base")
        stat, _diff = finish_worktree(self.root, path, branch, base, "luna team: interrupted work")
        return stat

    def close(self) -> None:
        """Remove the integration worktree; the team branch stays."""
        with _worktree_lock(self.root):
            _git(self.root, "worktree", "remove", "--force", str(self.integration))
            _git(self.root, "worktree", "prune")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/luna-core/tests/test_team_git.py -q`
Expected: PASS. On a git older than 2.24, `merge --no-verify` is unknown; `git --version` must show 2.24 or newer, which CI has.

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/team/git.py packages/luna-core/tests/test_team_git.py
git commit -m "feat(team): team branch, member worktrees, serial merges and verify"
```

---
### Task 7: The run report

**Files:**
- Create: `packages/luna-core/src/luna_core/team/report.py`
- Test: `packages/luna-core/tests/test_team_report.py`

**Interfaces:**
- Consumes: `TeamStore`, `TeamRun`, `TeamTask`, `LEAD` from `luna_core.team.board`.
- Produces:
  - `headline(run, tasks) -> str`;
  - `Report(markdown: str, data: dict)`;
  - `build_report(store, run_id, *, diffstat="", profile_changes=()) -> Report`;
  - `write_report(root, run_id, report) -> Path`, which writes `<root>/.luna/teams/<id>/report.md`;
  - `PROBLEM_KINDS`.
- Event kinds the report reads: these are the contract with the runner in Tasks 10–12.

  | Event | `data` fields |
  |---|---|
  | `team_denied` | `tool`, `preview`, `reason` |
  | `verify_failed` | `task`, `attempt` |
  | `conflict` | `task`, `fix` |
  | `markers` | `task`, `files` |
  | `interrupted` | `at` |
  | `provider_error` | `message` |
  | `stall_warn` | — |
  | `stalled` | — |
  | `lead_out_of_steps` | — |
  | `error` | `message` |
  | `decision` | `text` |

- [ ] **Step 1: Write the failing tests**

```python
"""The team run report."""

import pytest
from luna_core.team.board import LEAD, TeamStore
from luna_core.team.report import build_report, write_report


@pytest.fixture
def store(tmp_path):
    s = TeamStore(tmp_path / "s.db")
    yield s
    s.close()


def finished(store, outcome, reason=None):
    rid = store.create_run(workdir="/w", task="add flag", session_id="s", settings={})
    store.update_run(
        rid, status="finished", outcome=outcome, stop_reason=reason, started=100.0, finished=400.0
    )
    return rid


@pytest.mark.parametrize(
    ("outcome", "reason", "expected"),
    [
        ("done", None, "ГОТОВО"),
        ("partial", None, "ЧАСТИЧНО 1/3"),
        ("stopped", "budget", "ОСТАНОВЛЕНО (бюджет)"),
        ("stopped", "time", "ОСТАНОВЛЕНО (время)"),
        ("stopped", "cancel", "ОСТАНОВЛЕНО (отмена)"),
        ("stalled", None, "ЗАСТОЙ"),
        ("error", None, "ОШИБКА"),
    ],
)
def test_headline_for_every_outcome(store, outcome, reason, expected):
    rid = finished(store, outcome, reason)
    for title in ("a", "b", "c"):
        store.add_task(rid, title=title)
    store.update_task(rid, "T1", status="done")
    report = build_report(store, rid)
    assert report.data["headline"] == expected
    assert f"**Итог:** {expected}" in report.markdown


def test_report_lists_tasks_problems_decisions_and_cost(store, tmp_path):
    rid = finished(store, "partial")
    store.update_run(rid, branch="luna/team/x", base="abcdef123", plan={"goal": "flag"})
    store.add_member(rid, LEAD, {"role": "lead"})
    store.add_member(rid, "dev", {})
    store.update_member(rid, "dev", add_steps=7, add_cost=1.25, add_busy=120)
    store.add_task(rid, title="impl")
    store.update_task(
        rid, "T1", status="done", assignee="dev", note="wrote it", commits=["1234567890"]
    )
    store.add_task(rid, title="docs | api")
    store.update_task(rid, "T2", status="failed", note="ran out of steps")
    store.log_event(rid, "team_denied", "dev", tool="execute", preview="curl https://x", reason="profile")
    store.log_event(rid, "team_denied", "dev", tool="execute", preview="git push", reason="rule")
    store.log_event(rid, "verify_failed", "dev", task="T1", attempt=1)
    store.log_event(rid, "conflict", "dev", task="T1", fix="T3")
    store.log_event(rid, "interrupted", at="12:30")
    store.log_event(rid, "decision", LEAD, text="split T2")
    report = build_report(
        store, rid, diffstat=" b.txt | 1 +", profile_changes=["auto-profile tester v1: create"]
    )
    md = report.markdown
    assert "**Итог:** ЧАСТИЧНО 1/2" in md and "**Цель:** flag" in md
    assert "| T1 | impl | готово | dev | wrote it | 1234567 |" in md
    assert "| T2 | docs \\| api | провал | — | ran out of steps | — |" in md
    assert "- T2 docs | api — провал: ran out of steps" in md
    assert "⊘ dev: `curl https://x` — не в профиле" in md
    assert "⊘ dev: `git push` — запрещено правилом permissions.toml" in md
    assert "✗ T1: проверка не прошла после вливания (попытка 1)" in md
    assert "⚠ T1: конфликт при вливании → T3" in md
    assert "↻ прервано в 12:30, продолжено" in md
    assert "- split T2" in md
    assert "| dev | 7 | $1.25 | 2 мин |" in md and "5 мин" in md
    assert "`luna/team/x` (от `abcdef1`)" in md and " b.txt | 1 +" in md
    assert "git merge --no-ff luna/team/x" in md
    assert "- auto-profile tester v1: create" in md
    assert report.data["cost"] == 1.25 and report.data["merge_command"].endswith("luna/team/x")
    path = write_report(tmp_path, rid, report)
    assert path == tmp_path / ".luna" / "teams" / rid / "report.md"
    assert path.read_text(encoding="utf-8") == md


def test_quiet_run_says_so(store):
    rid = finished(store, "done")
    store.add_task(rid, title="a")
    store.update_task(rid, "T1", status="done")
    md = build_report(store, rid).markdown
    assert "Всё сделано." in md and "Ничего." in md and "Нет." in md
    assert "## Ветка" not in md and "## Профили субагентов" not in md
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_team_report.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna_core.team.report'`

- [ ] **Step 3: Implement `team/report.py`**

```python
"""The run report: ``.luna/teams/<id>/report.md`` and the same data as a dict."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from luna_core.team.board import TeamEvent, TeamRun, TeamStore, TeamTask

OUTCOME_TITLES = {
    "done": "ГОТОВО",
    "partial": "ЧАСТИЧНО",
    "stopped": "ОСТАНОВЛЕНО",
    "stalled": "ЗАСТОЙ",
    "error": "ОШИБКА",
}
STOP_REASONS = {"time": "время", "budget": "бюджет", "cancel": "отмена"}
STATUS_TITLES = {
    "pending": "ждёт",
    "in_progress": "в работе",
    "done": "готово",
    "failed": "провал",
    "blocked": "заблокирована",
}
PROBLEM_KINDS = frozenset(
    {"team_denied", "verify_failed", "conflict", "markers", "interrupted", "provider_error",
     "stall_warn", "stalled", "lead_out_of_steps", "error"}
)  # fmt: skip


@dataclass(frozen=True)
class Report:
    """The rendered report and its data (for ``luna team show`` and the TUI)."""

    markdown: str
    data: dict


def headline(run: TeamRun, tasks: list[TeamTask]) -> str:
    """``ГОТОВО`` | ``ЧАСТИЧНО n/m`` | ``ОСТАНОВЛЕНО (…)`` | ``ЗАСТОЙ`` | ``ОШИБКА``."""
    outcome = run.outcome or "error"
    if outcome == "partial":
        done = sum(t.status == "done" for t in tasks)
        return f"ЧАСТИЧНО {done}/{len(tasks)}"
    if outcome == "stopped":
        reason = STOP_REASONS.get(run.stop_reason or "", run.stop_reason or "?")
        return f"ОСТАНОВЛЕНО ({reason})"
    return OUTCOME_TITLES.get(outcome, OUTCOME_TITLES["error"])


def _cell(text: object) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _line(text: object) -> str:
    return " ".join(str(text).split())


def _minutes(seconds: float) -> str:
    whole = int(seconds // 60)
    return f"{whole} мин" if whole else f"{int(seconds)} с"


def _problem(event: TeamEvent) -> str:
    d = event.data
    if event.kind == "team_denied":
        why = "запрещено правилом permissions.toml" if d.get("reason") == "rule" else "не в профиле"
        return f"⊘ {event.member}: `{_line(d.get('preview', ''))}` — {why}"
    if event.kind == "verify_failed":
        return f"✗ {d.get('task')}: проверка не прошла после вливания (попытка {d.get('attempt')})"
    if event.kind == "conflict":
        return f"⚠ {d.get('task')}: конфликт при вливании → {d.get('fix')}"
    if event.kind == "markers":
        return f"⚠ {d.get('task')}: в файлах остались маркеры конфликта ({d.get('files')})"
    if event.kind == "interrupted":
        return f"↻ прервано в {d.get('at')}, продолжено"
    if event.kind == "provider_error":
        return f"✗ {event.member}: ошибка провайдера: {_line(d.get('message', ''))}"
    if event.kind == "stall_warn":
        return "… доска не менялась несколько пробуждений подряд — лидеру сказано действовать"
    if event.kind == "stalled":
        return "… застой: доска так и не изменилась, запуск остановлен"
    if event.kind == "lead_out_of_steps":
        return "… у лидера кончились шаги"
    return f"✗ {_line(d.get('message', ''))}"


def build_report(
    store: TeamStore,
    run_id: str,
    *,
    diffstat: str = "",
    profile_changes: Iterable[str] = (),
) -> Report:
    """Render the report of a run from the board and its events."""
    run = store.run(run_id)
    if run is None:
        raise KeyError(f"no team run {run_id}")
    tasks = store.tasks(run_id)
    members = store.members(run_id)
    events = store.events(run_id)
    title = headline(run, tasks)
    problems = [_problem(e) for e in events if e.kind in PROBLEM_KINDS]
    decisions = [_line(e.data.get("text", "")) for e in events if e.kind == "decision"]
    changes = [_line(c) for c in profile_changes]
    cost = sum(m.cost for m in members)
    duration = ((run.finished or time.time()) - run.started) if run.started else 0.0
    merge = f"git merge --no-ff {run.branch}" if run.branch else ""

    lines = [f"# Команда {run.id}", "", f"**Задача:** {_line(run.task)}", ""]
    if run.plan and run.plan.get("goal"):
        lines += [f"**Цель:** {_line(run.plan['goal'])}", ""]
    lines += [f"**Итог:** {title}", "", f"**Время:** {_minutes(duration)} · "
              f"**Стоимость:** ${cost:.2f}", "", "## Задачи", ""]  # fmt: skip
    if tasks:
        lines += [
            "| | Задача | Статус | Кто | Результат | Коммиты |",
            "|---|---|---|---|---|---|",
        ]
        for t in tasks:
            commits = ", ".join(c[:7] for c in t.commits) or "—"
            lines.append(
                f"| {t.id} | {_cell(t.title)} | {STATUS_TITLES[t.status]} | {t.assignee or '—'} "
                f"| {_cell(t.note[:200]) or '—'} | {commits} |"
            )
    else:
        lines.append("Задач нет.")
    undone = [t for t in tasks if t.status != "done"]
    lines += ["", "## Не сделано", ""]
    lines += [
        f"- {t.id} {_line(t.title)} — {STATUS_TITLES[t.status]}: "
        f"{_line(t.note[:300]) or 'без пояснения'}"
        for t in undone
    ] or ["Всё сделано."]
    lines += ["", "## Что пошло не так", ""] + ([f"- {p}" for p in problems] or ["Ничего."])
    lines += ["", "## Решения лидера", ""] + ([f"- {d}" for d in decisions] or ["Нет."])
    lines += ["", "## Участники", "", "| Участник | Шаги | Стоимость | Время работы |",
              "|---|---|---|---|"]  # fmt: skip
    lines += [f"| {m.name} | {m.steps} | ${m.cost:.2f} | {_minutes(m.busy)} |" for m in members]
    if run.branch:
        lines += ["", "## Ветка", "", f"`{run.branch}` (от `{(run.base or '')[:7]}`)", ""]
        if diffstat:
            lines += ["```", diffstat, "```", ""]
        lines.append(f"Слить в свою ветку: `{merge}`")
    if changes:
        lines += ["", "## Профили субагентов", ""] + [f"- {c}" for c in changes]

    data = {
        "id": run.id,
        "task": run.task,
        "outcome": run.outcome,
        "stop_reason": run.stop_reason,
        "headline": title,
        "branch": run.branch,
        "base": run.base,
        "merge_command": merge,
        "diffstat": diffstat,
        "cost": cost,
        "duration": duration,
        "tasks": [
            {"id": t.id, "title": t.title, "kind": t.kind, "status": t.status,
             "assignee": t.assignee, "note": t.note, "commits": list(t.commits)}
            for t in tasks
        ],  # fmt: skip
        "members": [
            {"name": m.name, "steps": m.steps, "cost": m.cost, "busy": m.busy} for m in members
        ],
        "problems": problems,
        "decisions": decisions,
        "profile_changes": changes,
    }
    return Report("\n".join(lines) + "\n", data)


def write_report(root: str | Path, run_id: str, report: Report) -> Path:
    """Write ``.luna/teams/<id>/report.md`` under ``root`` and return its path."""
    path = Path(root) / ".luna" / "teams" / run_id / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.markdown, encoding="utf-8")
    return path
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/luna-core/tests/test_team_report.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/team/report.py packages/luna-core/tests/test_team_report.py
git commit -m "feat(team): run report with outcome, problems, decisions, cost and branch"
```

---

### Task 8: Keep the machine awake

**Files:**
- Create: `packages/luna-core/src/luna_core/team/inhibit.py`
- Test: `packages/luna-core/tests/test_team_inhibit.py`

**Interfaces:**
- Consumes: `kill_tree` from `luna_core.shell`.
- Produces: `keep_awake(enabled=True, *, platform=sys.platform, which=shutil.which, popen=subprocess.Popen, kernel32=None) -> ContextManager[bool]`. It yields `True` while sleep is held off. When disabled, unavailable or failing, it yields `False` and never raises.

- [ ] **Step 1: Write the failing tests**

```python
"""Keeping the machine awake during a team run."""

import os

from luna_core.team import inhibit
from luna_core.team.inhibit import keep_awake


class FakeProc:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.pid = 4242
        self.waited = False

    def wait(self, timeout=None):
        self.waited = True
        return 0


def spawner(started):
    def popen(argv, **kwargs):
        proc = FakeProc(argv, **kwargs)
        started.append(proc)
        return proc

    return popen


def test_macos_uses_caffeinate_tied_to_our_pid(monkeypatch):
    killed, started = [], []
    monkeypatch.setattr(inhibit, "kill_tree", killed.append)
    with keep_awake(
        platform="darwin", which=lambda name: f"/usr/bin/{name}", popen=spawner(started)
    ) as held:
        assert held is True
        assert started[0].argv == ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]
    assert killed == [4242] and started[0].waited


def test_linux_uses_systemd_inhibit_when_present(monkeypatch):
    monkeypatch.setattr(inhibit, "kill_tree", lambda pid: None)
    started = []
    with keep_awake(platform="linux", which=lambda name: f"/bin/{name}", popen=spawner(started)):
        assert started[0].argv[0] == "/bin/systemd-inhibit"
        assert "--what=idle:sleep" in started[0].argv


def test_nothing_available_or_disabled_yields_false():
    with keep_awake(platform="linux", which=lambda name: None) as held:
        assert held is False
    with keep_awake(False, platform="darwin", which=lambda name: "/x") as held:
        assert held is False


def test_a_failing_spawn_is_not_fatal():
    def broken(argv, **kwargs):
        raise OSError("no such file")

    with keep_awake(platform="darwin", which=lambda name: "/x", popen=broken) as held:
        assert held is False


def test_windows_sets_and_clears_the_execution_state():
    calls = []

    class Kernel32:
        def SetThreadExecutionState(self, flags):  # noqa: N802 - Win32 name
            calls.append(flags)
            return 1

    with keep_awake(platform="win32", kernel32=Kernel32()) as held:
        assert held is True
    assert calls == [
        inhibit.ES_CONTINUOUS | inhibit.ES_SYSTEM_REQUIRED,
        inhibit.ES_CONTINUOUS,
    ]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_team_inhibit.py -q`
Expected: FAIL — `ImportError: cannot import name 'inhibit'`

- [ ] **Step 3: Implement `team/inhibit.py`**

```python
"""Keep the machine awake while a team runs (``--allow-sleep`` turns it off).

macOS: ``caffeinate -i -w <pid>``; Linux: ``systemd-inhibit`` when present;
Windows: ``SetThreadExecutionState``. Anything missing or failing only
means the machine may sleep: it never stops the run.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator

from luna_core.shell import kill_tree

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _argv(platform: str, which: Callable[[str], str | None]) -> list[str] | None:
    if platform == "darwin":
        exe = which("caffeinate")
        return [exe, "-i", "-w", str(os.getpid())] if exe else None
    exe = which("systemd-inhibit")
    if exe:
        return [exe, "--what=idle:sleep", "--who=luna", "--why=luna team run", "--mode=block",
                "sleep", "infinity"]  # fmt: skip
    return None


def _kernel32() -> object | None:
    try:
        import ctypes

        return ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        return None


@contextlib.contextmanager
def keep_awake(
    enabled: bool = True,
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    kernel32: object | None = None,
) -> Iterator[bool]:
    """Hold off system sleep inside the block; yield whether that worked."""
    if not enabled:
        yield False
        return
    if platform == "win32":
        k32 = kernel32 if kernel32 is not None else _kernel32()
        if k32 is None:
            yield False
            return
        k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        try:
            yield True
        finally:
            k32.SetThreadExecutionState(ES_CONTINUOUS)
        return
    argv = _argv(platform, which)
    if argv is None:
        yield False
        return
    try:
        proc = popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(Exception):
            kill_tree(proc.pid)
            proc.wait(timeout=5)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/luna-core/tests/test_team_inhibit.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/team/inhibit.py packages/luna-core/tests/test_team_inhibit.py
git commit -m "feat(team): keep the machine awake during a run"
```

---

### Task 9: Board and message tools

**Files:**
- Create: `packages/luna-core/src/luna_core/team/tools.py`
- Test: `packages/luna-core/tests/test_team_tools.py`

**Interfaces:**
- Consumes: `TeamStore`, `LEAD`, `TASK_KINDS` (Task 3); `Mailbox` (Task 3).
- Produces:
  - `TeamContext(store, mailbox, run_id, member, task_id=None, stopping=lambda: False, hire=None, on_change=lambda: None, reports={})`, with method `decision(text)` that logs a `decision` event;
  - `render_board(store, run_id) -> str`;
  - `member_tools(ctx) -> list[Tool]`: `board_list`, `board_update`, `send_message`, `read_messages`;
  - `lead_tools(ctx) -> list[Tool]`: `board_list`, `send_message`, `read_messages`, `board_add`, `board_split`, `hire`, `retire`, `reassign`, `broadcast`;
  - `STOP_NOTE` (the prefix of every tool result while the run is stopping);
  - `REPORT_STATUSES = ("done", "failed", "blocked")`.
- Contract with the runner:
  - `board_update` only records `ctx.reports[task_id] = (status, note)`; the runner sets the real status after the commit and merge.
  - `hire` calls `ctx.hire(args) -> str`, which raises `ValueError` / `LunaCoreError` on a bad request or the member limit.

- [ ] **Step 1: Write the failing tests**

```python
"""Team board and message tools."""

import pytest
from luna_core.team.board import LEAD, TeamStore
from luna_core.team.messages import Mailbox
from luna_core.team.tools import TeamContext, lead_tools, member_tools
from luna_core.tools.base import ToolContext


@pytest.fixture
def board(tmp_path):
    store = TeamStore(tmp_path / "s.db")
    rid = store.create_run(workdir="/w", task="t", session_id="s", settings={})
    store.add_member(rid, LEAD, {"role": "lead"})
    store.add_member(rid, "dev", {})
    store.add_member(rid, "ops", {})
    store.add_task(rid, title="first")
    store.add_task(rid, title="second", deps=["T1"])
    yield store, rid
    store.close()


def by_name(tools):
    return {t.name: t for t in tools}


async def call(tool, **args):
    out = await tool.invoke(ToolContext(), args)
    return out.is_error, out.content[0].text


async def test_member_tools(board):
    store, rid = board
    ctx = TeamContext(store, Mailbox(store), rid, "dev", task_id="T1")
    tools = by_name(member_tools(ctx))
    assert set(tools) == {"board_list", "board_update", "send_message", "read_messages"}
    _err, text = await call(tools["board_list"])
    assert "T1 [pending] first (code)" in text and "T2 [pending] second (code) deps T1" in text
    assert "members: dev (idle), ops (idle)" in text
    err, _text = await call(tools["board_update"], status="done", note="did it")
    assert not err and ctx.reports == {"T1": ("done", "did it")}
    err, _text = await call(tools["board_update"], status="weird", note="")
    assert err
    err, _text = await call(tools["send_message"], to="ops", text="hi")
    assert not err
    err, text = await call(tools["send_message"], to="*", text="all")
    assert err and "only the lead" in text
    ops = by_name(member_tools(TeamContext(store, Mailbox(store), rid, "ops")))
    assert (await call(ops["read_messages"]))[1] == "from dev: hi"
    assert (await call(ops["read_messages"]))[1] == "no new messages"
    err, text = await call(ops["board_update"], status="done", note="x")
    assert err and "no task" in text


async def test_every_result_carries_the_stop_note_while_stopping(board):
    store, rid = board
    ctx = TeamContext(store, Mailbox(store), rid, "dev", task_id="T1", stopping=lambda: True)
    _err, text = await call(by_name(member_tools(ctx))["board_list"])
    assert text.startswith("STOP:")


async def test_lead_tools_change_the_board_and_log_decisions(board):
    store, rid = board
    changes = []
    ctx = TeamContext(
        store,
        Mailbox(store),
        rid,
        LEAD,
        on_change=lambda: changes.append(1),
        hire=lambda args: f"hired {args['name']}",
    )
    tools = by_name(lead_tools(ctx))
    assert "board_update" not in tools
    assert {"board_add", "board_split", "hire", "retire", "reassign", "broadcast"} <= set(tools)
    _err, text = await call(
        tools["board_add"], title="third", kind="research", deps=["T1"], member="ops"
    )
    assert text == "added T3" and store.task(rid, "T3").assignee == "ops"
    _err, text = await call(tools["board_split"], task_id="T2", parts=[{"title": "a"}, {"title": "b"}])
    assert text == "T2 split into T4, T5"
    assert store.task(rid, "T2") is None and store.task(rid, "T4").deps == ("T1",)
    await call(tools["reassign"], task_id="T4", member="dev")
    assert store.task(rid, "T4").assignee == "dev"
    await call(tools["retire"], member="dev")
    assert store.member(rid, "dev").status == "retired" and store.task(rid, "T4").assignee is None
    err, _text = await call(tools["reassign"], task_id="T5", member="dev")
    assert err
    err, _text = await call(tools["board_split"], task_id="T4", parts=[{"title": "only"}])
    assert err
    _err, text = await call(tools["broadcast"], text="wrap up")
    assert text == "sent to 1 members"
    _err, text = await call(tools["hire"], name="qa", prompt="test things")
    assert text == "hired qa"
    decisions = [e.data["text"] for e in store.events(rid, kinds=["decision"])]
    assert decisions == ["added T3: third", "split T2 into T4, T5", "T4 → dev", "retired dev"]
    assert changes


async def test_a_working_member_cannot_be_retired(board):
    store, rid = board
    store.claim_next(rid, "dev")
    store.update_member(rid, "dev", status="working")
    ctx = TeamContext(store, Mailbox(store), rid, LEAD)
    err, text = await call(by_name(lead_tools(ctx))["retire"], member="dev")
    assert err and "working" in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_team_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna_core.team.tools'`

- [ ] **Step 3: Implement `team/tools.py`**

```python
"""Board and message tools for team members and the lead."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from luna_core.errors import LunaCoreError
from luna_core.team.board import LEAD, TASK_KINDS, TeamStore
from luna_core.team.messages import Mailbox
from luna_core.tools.base import Tool, ToolContext, ToolOutput

#: What a member may report with ``board_update``.
REPORT_STATUSES = ("done", "failed", "blocked")
#: Prefixed to every tool result once the run is stopping.
STOP_NOTE = (
    "STOP: the run is stopping — save your work, report with board_update and end your turn.\n\n"
)
_OPEN = ("pending", "blocked", "failed")


@dataclass
class TeamContext:
    """What the board tools of one member (or the lead) work on."""

    store: TeamStore
    mailbox: Mailbox
    run_id: str
    member: str
    task_id: str | None = None
    stopping: Callable[[], bool] = lambda: False
    hire: Callable[[dict], str] | None = None
    on_change: Callable[[], None] = lambda: None
    reports: dict[str, tuple[str, str]] = field(default_factory=dict)

    def decision(self, text: str) -> None:
        """Record a lead decision for the report."""
        self.store.log_event(self.run_id, "decision", self.member, text=text)


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required)}


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _ids(args: dict, key: str) -> list[str]:
    value = args.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} must be a list of task ids")
    return value


def render_board(store: TeamStore, run_id: str) -> str:
    """The board as text: one line per task, then the members."""
    lines = []
    for t in store.tasks(run_id):
        deps = f" deps {', '.join(t.deps)}" if t.deps else ""
        owner = f" → {t.assignee}" if t.assignee else ""
        note = f": {' '.join(t.note.split())[:200]}" if t.note else ""
        lines.append(f"{t.id} [{t.status}] {t.title} ({t.kind}){deps}{owner}{note}")
    members = [f"{m.name} ({m.status})" for m in store.members(run_id) if m.name != LEAD]
    lines.append("members: " + (", ".join(members) or "none"))
    return "\n".join(lines)


def _tool(
    ctx: TeamContext, name: str, description: str, schema: dict, fn: Callable[[dict], str]
) -> Tool:
    async def run(tool_ctx: ToolContext, args: dict) -> ToolOutput:
        try:
            text = fn(args)
        except KeyError as exc:
            return ToolOutput.error(f"{name}: {exc.args[0] if exc.args else exc}")
        except (ValueError, LunaCoreError) as exc:
            return ToolOutput.error(f"{name}: {exc}")
        return ToolOutput.text((STOP_NOTE if ctx.stopping() else "") + text)

    return Tool(name, description, schema, run, raw=True)


def _common(ctx: TeamContext) -> list[Tool]:
    def board_list(args: dict) -> str:
        return render_board(ctx.store, ctx.run_id)

    def send_message(args: dict) -> str:
        to = _text(args, "to")
        ctx.mailbox.send(ctx.run_id, ctx.member, to, _text(args, "text"))
        ctx.on_change()
        return f"sent to {to}"

    def read_messages(args: dict) -> str:
        messages = ctx.mailbox.unread(ctx.run_id, ctx.member)
        return "\n".join(f"from {m.sender}: {m.text}" for m in messages) or "no new messages"

    return [
        _tool(ctx, "board_list", "Show the team board: tasks, statuses, owners, members.",
              _schema({}), board_list),
        _tool(ctx, "send_message",
              "Send a message to one teammate or to 'lead'. Only the lead can message everybody.",
              _schema({"to": {"type": "string"}, "text": {"type": "string"}}, ("to", "text")),
              send_message),
        _tool(ctx, "read_messages", "Read your unread messages.", _schema({}), read_messages),
    ]  # fmt: skip


def member_tools(ctx: TeamContext) -> list[Tool]:
    """The tools of a member working one task."""

    def board_update(args: dict) -> str:
        status = args.get("status")
        if status not in REPORT_STATUSES:
            raise ValueError(f"status must be one of {', '.join(REPORT_STATUSES)}")
        if ctx.task_id is None:
            raise ValueError("you have no task to report on")
        note = str(args.get("note") or "").strip()
        ctx.reports[ctx.task_id] = (status, note)
        ctx.on_change()
        return f"{ctx.task_id} reported as {status}; end your turn now"

    update = _tool(
        ctx,
        "board_update",
        "Report your task's result: done, failed or blocked, with a short note.",
        _schema(
            {"status": {"type": "string", "enum": list(REPORT_STATUSES)},
             "note": {"type": "string"}},
            ("status", "note"),
        ),  # fmt: skip
        board_update,
    )
    common = _common(ctx)
    return [common[0], update, *common[1:]]


def lead_tools(ctx: TeamContext) -> list[Tool]:
    """The lead's tools: the common ones plus board and team changes."""
    store, run = ctx.store, ctx.run_id

    def active(name: str) -> None:
        member = store.member(run, name)
        if member is None or name == LEAD or member.status == "retired":
            raise ValueError(f"no active member {name!r}")

    def board_add(args: dict) -> str:
        kind = args.get("kind") or "code"
        member = args.get("member") or None
        if member is not None:
            active(member)
        task = store.add_task(
            run,
            title=_text(args, "title"),
            kind=kind,
            detail=str(args.get("detail") or ""),
            deps=_ids(args, "deps"),
            assignee=member,
        )
        store.log_event(run, "team_task", LEAD, task=task.id, status=task.status)
        ctx.decision(f"added {task.id}: {task.title}")
        ctx.on_change()
        return f"added {task.id}"

    def board_split(args: dict) -> str:
        task_id = _text(args, "task_id")
        task = store.task(run, task_id)
        if task is None:
            raise ValueError(f"no task {task_id}")
        if task.status not in _OPEN:
            raise ValueError(f"{task_id} is {task.status}; only pending, blocked or failed tasks split")
        parts = args.get("parts")
        if not isinstance(parts, list) or len(parts) < 2 or not all(isinstance(p, dict) for p in parts):
            raise ValueError("parts must list at least two tasks, each an object with a title")
        titles = [_text(p, "title") for p in parts]
        made = [
            store.add_task(
                run,
                title=title,
                kind=part.get("kind") or task.kind,
                detail=str(part.get("detail") or ""),
                deps=task.deps,
            ).id
            for title, part in zip(titles, parts, strict=True)
        ]
        store.remove_task(run, task_id, replace_with=made)
        ctx.decision(f"split {task_id} into {', '.join(made)}")
        ctx.on_change()
        return f"{task_id} split into {', '.join(made)}"

    def hire(args: dict) -> str:
        if ctx.hire is None:
            raise ValueError("hiring is not available here")
        text = ctx.hire({k: v for k, v in args.items() if v is not None})
        ctx.on_change()
        return text

    def retire(args: dict) -> str:
        name = _text(args, "member")
        member = store.member(run, name)
        if member is None or name == LEAD:
            raise ValueError(f"no member {name!r}")
        if member.status == "working":
            raise ValueError(f"{name} is working; retire it once it is idle")
        if member.status == "retired":
            return f"{name} is already retired"
        store.update_member(run, name, status="retired")
        for t in store.tasks(run):
            if t.assignee == name and t.status in _OPEN:
                store.update_task(run, t.id, assignee=None)
        store.log_event(run, "team_member", LEAD, member=name, state="retired")
        ctx.decision(f"retired {name}")
        ctx.on_change()
        return f"{name} retired"

    def reassign(args: dict) -> str:
        task_id = _text(args, "task_id")
        member = args.get("member") or None
        task = store.task(run, task_id)
        if task is None:
            raise ValueError(f"no task {task_id}")
        if task.status not in _OPEN:
            raise ValueError(f"{task_id} is {task.status}; only pending, blocked or failed tasks move")
        if member is not None:
            active(member)
        store.update_task(run, task_id, assignee=member, status="pending")
        store.log_event(run, "team_task", LEAD, task=task_id, status="pending", assignee=member)
        ctx.decision(f"{task_id} → {member or 'anyone'}")
        ctx.on_change()
        return f"{task_id} is pending for {member or 'anyone'}"

    def broadcast(args: dict) -> str:
        count = ctx.mailbox.broadcast(run, LEAD, _text(args, "text"))
        return f"sent to {count} members"

    string, ids = {"type": "string"}, {"type": "array", "items": {"type": "string"}}
    return [
        *_common(ctx),
        _tool(ctx, "board_add", "Add a task to the board (kind code or research).",
              _schema({"title": string, "kind": {"type": "string", "enum": list(TASK_KINDS)},
                       "detail": string, "deps": ids, "member": string}, ("title",)),
              board_add),
        _tool(ctx, "board_split", "Replace a pending, blocked or failed task by smaller tasks.",
              _schema({"task_id": string,
                       "parts": {"type": "array", "items": _schema(
                           {"title": string, "kind": string, "detail": string}, ("title",))}},
                      ("task_id", "parts")),
              board_split),
        _tool(ctx, "hire", "Hire a member: a profile name or a prompt, tools, skills, model.",
              _schema({"name": string, "profile": string, "prompt": string, "tools": ids,
                       "skills": ids, "model": string}, ("name",)),
              hire),
        _tool(ctx, "retire", "Retire an idle member; its open tasks become unassigned.",
              _schema({"member": string}, ("member",)), retire),
        _tool(ctx, "reassign", "Give a pending, blocked or failed task to a member (or anyone).",
              _schema({"task_id": string, "member": string}, ("task_id",)), reassign),
        _tool(ctx, "broadcast", "Message every member at once (lead only).",
              _schema({"text": string}, ("text",)), broadcast),
    ]  # fmt: skip
```

- [ ] **Step 4: Run the tests and the linters**

Run: `uv run pytest packages/luna-core/tests/test_team_tools.py -q && uv run ruff check packages/luna-core/src/luna_core/team/tools.py`
Expected: PASS. If ruff reports E501 on the two long `raise ValueError(...)` lines in `board_split` / `reassign`, wrap the f-string over two lines.

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/team/tools.py packages/luna-core/tests/test_team_tools.py
git commit -m "feat(team): board and message tools for members and the lead"
```

---
### Task 10: The team runner — members, merges, lead, report

**Files:**
- Create: `packages/luna-core/src/luna_core/team/runner.py`
- Create: `packages/luna-core/tests/team_kit.py`
- Test: `packages/luna-core/tests/test_team_runner.py`

**Interfaces:**
- Consumes: everything from Tasks 1–9. The exact names are in their Interfaces blocks.
- Produces:
  - `TeamHost` (dataclass) with fields `provider`, `model`, `tools`, `roles=None`, `profiles={}`, `skill_names=()`, `skill_loader=None`, `base_rules=None`, `sandbox=None`, `hooks=None`, `model_overrides=None`, `journal=None`, `gate=None`, `after_run=None`;
  - `TeamRunner(store, log, host, root, settings=None, *, clock=time.time, tick=0.5, keep_awake=keep_awake)` with these methods:
    - `create(task, *, budget=None) -> run_id`;
    - `async plan(run_id, *, feedback=None) -> TeamPlan`;
    - `accept(run_id, *, plan=None, allow=None, budget=None) -> TeamPlan`;
    - `decline(run_id)`;
    - `async run(run_id) -> outcome`;
    - `member_spec(member) -> SubagentSpec`;
    - `close()`.
- Run contract:
  - A run lives in `TeamStore`.
  - The lead is the member row `"lead"`.
  - Lead requests carry `Team lead` in the system prompt.
  - Member requests carry `Team member: <name>`.
  - `run()` ends with the run `finished`, `report.md` written and a `team_done` event.
- Not in this task:
  - limits, stall detection and `cancel` come in Task 11;
  - recovery after a restart, `resume` and the early failure path come in Task 12.

- [ ] **Step 1: Write the test helpers `packages/luna-core/tests/team_kit.py`**

```python
"""Shared helpers for the team runner tests (imported as ``team_kit``)."""

from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
import time
from pathlib import Path

from luna_core.messages import ToolResult
from luna_core.providers.fake import FakeProvider, _events_for
from luna_core.rategate import RateGate
from luna_core.roles import ModelRoles
from luna_core.session import EventLog
from luna_core.team.board import TeamStore
from luna_core.team.runner import TeamHost, TeamRunner
from luna_core.team.settings import TeamSettings
from luna_core.tools import builtin_tools

_MEMBER = re.compile(r"Team member: ([a-z0-9-]+)")
call, text = FakeProvider.call, FakeProvider.text


class TeamProvider:
    """Routes each request to the script of whoever asks: ``lead`` or a member name.

    An exhausted script answers "nothing to do" (the lead is woken a variable
    number of times). ``before(who, request)`` runs first and may await;
    ``delay`` slows every request so member turns overlap.
    """

    name = "fake"

    def __init__(self, routes: dict, *, delay: float = 0.0, before=None) -> None:
        self.routes = {k: list(v) for k, v in routes.items()}
        self.requests: dict[str, list] = {}
        self.delay = delay
        self.before = before

    @staticmethod
    def who(request) -> str:
        if "Team lead" in request.system:
            return "lead"
        found = _MEMBER.search(request.system)
        return found.group(1) if found else "?"

    async def stream(self, request):
        who = self.who(request)
        self.requests.setdefault(who, []).append(request)
        if self.before is not None:
            await self.before(who, request)
        if self.delay:
            await asyncio.sleep(self.delay)
        script = self.routes.get(who) or []
        item = script.pop(0) if script else FakeProvider.text("nothing to do")
        for event in item if isinstance(item, list) else _events_for(item):
            if isinstance(event, BaseException):
                raise event
            yield event


def done(note: str):
    """A member reporting its task done."""
    return call("board_update", {"status": "done", "note": note}, "u")


def git(root, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    return root


def plan_dict(tasks, members=None, allow=()) -> dict:
    return {
        "goal": "g",
        "criteria": ["c"],
        "tasks": list(tasks),
        "members": list(members or [{"name": "dev", "prompt": "You write code."}]),
        "allow": list(allow),
    }


def make_runner(
    tmp_path,
    provider,
    *,
    settings=None,
    clock=None,
    overrides=None,
    base_rules=None,
    journal=None,
    profiles=None,
    keep_awake=None,
):
    db = tmp_path / "db" / "s.db"
    log, store = EventLog(db), TeamStore(db)
    host = TeamHost(
        provider=provider,
        model="main-model",
        tools=builtin_tools(),
        roles=ModelRoles(strong="lead-model"),
        profiles=profiles or {},
        base_rules=base_rules,
        model_overrides=overrides,
        journal=journal,
        gate=RateGate(1_000_000),
    )
    runner = TeamRunner(
        store,
        log,
        host,
        tmp_path / "repo",
        settings or TeamSettings(),
        clock=clock or time.time,
        tick=0.01,
        keep_awake=keep_awake or (lambda enabled: contextlib.nullcontext(False)),
    )
    return runner, store, log


def ready_run(runner, store, plan) -> str:
    """A run with ``plan`` accepted, ready for ``runner.run``."""
    rid = runner.create("do the thing")
    store.update_run(rid, status="awaiting_plan", plan=plan)
    runner.accept(rid)
    return rid


def result_of(request, call_id: str) -> ToolResult | None:
    for message in request.messages:
        for block in message.content:
            if isinstance(block, ToolResult) and block.call_id == call_id:
                return block
    return None


def report_of(repo: Path, rid: str) -> str:
    return (repo / ".luna" / "teams" / rid / "report.md").read_text(encoding="utf-8")
```

- [ ] **Step 2: Write the failing tests `packages/luna-core/tests/test_team_runner.py`**

```python
"""The team runner: members, merges, the lead and the report."""

import subprocess

import pytest
from luna_core.assemblies import AssemblyLog
from luna_core.policy import Rules
from luna_core.team.settings import TeamSettings
from team_kit import (
    TeamProvider,
    call,
    done,
    git,
    make_repo,
    make_runner,
    plan_dict,
    ready_run,
    report_of,
    result_of,
    text,
)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


async def test_team_merges_code_writes_notes_and_reports(repo, tmp_path):
    (repo / "mine.txt").write_text("user work\n")
    provider = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
                call("read_file", {"file_path": "b.txt"}, "r1"),
                done("b holds one line"),
                text("done"),
            ]
        }
    )
    journal = AssemblyLog(tmp_path / "db" / "s.db")
    runner, store, log = make_runner(tmp_path, provider, journal=journal)
    rid = ready_run(
        runner,
        store,
        plan_dict(
            [
                {"id": "T1", "title": "write b", "kind": "code", "member": "dev"},
                {"id": "T2", "title": "check b", "kind": "research", "deps": ["T1"], "member": "dev"},
            ]
        ),
    )
    head = git(repo, "rev-parse", "HEAD")
    assert await runner.run(rid) == "done"
    assert git(repo, "show", f"luna/team/{rid}:b.txt") == "b\n"
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "branch", "--show-current").strip() == "main"
    assert not (repo / "b.txt").exists() and (repo / "mine.txt").read_text() == "user work\n"
    note = repo / ".luna" / "teams" / rid / "notes" / "T2.md"
    assert "b holds one line" in note.read_text(encoding="utf-8")
    report = report_of(repo, rid)
    assert "**Итог:** ГОТОВО" in report and f"git merge --no-ff luna/team/{rid}" in report
    assert list((repo / ".luna" / "worktrees").glob("team-*")) == []
    assert git(repo, "branch", "--list", "luna/teamwork/*").strip() == ""
    assert provider.requests["lead"][0].model == "lead-model"
    assert provider.requests["dev"][0].model == "main-model"
    assert {t.name for t in provider.requests["dev"][3].tools} >= {"read_file", "board_update"}
    assert "write_file" not in {t.name for t in provider.requests["dev"][3].tools}
    assert [r.base_profile for r in journal.pending()] == ["team", "team"]
    run = store.run(rid)
    assert (run.status, run.outcome) == ("finished", "done")
    assert len(store.events(rid, kinds=["team_done"])) == 1
    journal.close()
    log.close()
    store.close()


def writer(content: str) -> list:
    return [
        call("read_file", {"file_path": "a.txt"}, "r"),
        call("write_file", {"file_path": "a.txt", "content": content}, "w"),
        done(content.strip()),
        text("done"),
    ]


async def test_a_merge_conflict_becomes_a_resolve_task(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": writer("from dev\n") + writer("both\n"),
            "ops": writer("from ops\n") + writer("both\n"),
        },
        delay=0.05,
    )
    runner, store, log = make_runner(tmp_path, provider, settings=TeamSettings(max_concurrent=2))
    rid = ready_run(
        runner,
        store,
        plan_dict(
            [
                {"id": "T1", "title": "dev edit", "member": "dev"},
                {"id": "T2", "title": "ops edit", "member": "ops"},
            ],
            members=[{"name": "dev", "prompt": "You edit."}, {"name": "ops", "prompt": "You edit."}],
        ),
    )
    assert await runner.run(rid) == "done"
    tasks = store.tasks(rid)
    assert [t.status for t in tasks] == ["done", "done", "done"]
    fix = tasks[2]
    assert fix.merge_of in ("T1", "T2") and fix.title.startswith("resolve merge conflict")
    assert git(repo, "show", f"luna/team/{rid}:a.txt") == "both\n"
    assert len(store.events(rid, kinds=["conflict"])) == 1
    assert "конфликт при вливании" in report_of(repo, rid)
    log.close()
    store.close()


VERIFY = "if [ -f bad.txt ]; then echo bad file present; exit 1; fi"


async def test_red_verify_sends_the_task_back_with_the_output(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "bad.txt", "content": "x\n"}, "w1"),
                done("first try"),
                text("done"),
                call("delete", {"file_path": "bad.txt"}, "d1"),
                done("removed it"),
                text("done"),
            ]
        }
    )
    settings = TeamSettings(verify_command=VERIFY, max_attempts=2)
    runner, store, log = make_runner(tmp_path, provider, settings=settings)
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "tidy", "member": "dev"}]))
    assert await runner.run(rid) == "done"
    assert len(store.events(rid, kinds=["verify_failed"])) == 1
    second = provider.requests["dev"][3].messages[0].text
    assert "verify failed after merging" in second and "bad file present" in second
    shown = subprocess.run(
        ["git", "cat-file", "-e", f"luna/team/{rid}:bad.txt"], cwd=repo, capture_output=True
    )
    assert shown.returncode != 0
    assert store.task(rid, "T1").attempts == 2
    log.close()
    store.close()


async def test_denied_commands_land_in_the_report(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("execute", {"command": "curl https://example.com"}, "e1"),
                call("execute", {"command": "git push origin main"}, "e2"),
                call("execute", {"command": "git status"}, "e3"),
                done("checked"),
                text("done"),
            ]
        }
    )
    runner, store, log = make_runner(
        tmp_path, provider, base_rules=Rules(deny=["execute:git push*"])
    )
    rid = ready_run(
        runner,
        store,
        plan_dict([{"id": "T1", "title": "look", "member": "dev"}], allow=["execute:git *"]),
    )
    assert await runner.run(rid) == "done"
    denied = [(e.data["preview"], e.data["reason"]) for e in store.events(rid, kinds=["team_denied"])]
    assert denied == [("curl https://example.com", "profile"), ("git push origin main", "rule")]
    report = report_of(repo, rid)
    assert "⊘ dev: `curl https://example.com` — не в профиле" in report
    assert "⊘ dev: `git push origin main` — запрещено правилом permissions.toml" in report
    assert result_of(provider.requests["dev"][3], "e3").is_error is False
    log.close()
    store.close()


async def test_lead_wakes_on_messages_and_changes_and_can_add_work(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("send_message", {"to": "lead", "text": "T1 needs a README too"}, "m1"),
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("b"),
                text("done"),
                call("write_file", {"file_path": "README.md", "content": "# b\n"}, "w2"),
                done("readme"),
                text("done"),
            ],
            "lead": [
                call("board_add", {"title": "write README", "kind": "code", "member": "dev"}, "a1"),
                text("added"),
            ],
        }
    )
    runner, store, log = make_runner(tmp_path, provider)
    rid = ready_run(
        runner, store, plan_dict([{"id": "T1", "title": "write b", "kind": "code", "member": "dev"}])
    )
    assert await runner.run(rid) == "done"
    assert [t.id for t in store.tasks(rid)] == ["T1", "T2"]
    assert git(repo, "show", f"luna/team/{rid}:README.md") == "# b\n"
    first_wake = provider.requests["lead"][0].messages[-1].text
    assert "from dev: T1 needs a README too" in first_wake and "Board:" in first_wake
    assert "Team lead" in provider.requests["lead"][0].system
    decisions = [e.data["text"] for e in store.events(rid, kinds=["decision"])]
    assert decisions == ["added T2: write README"]
    log.close()
    store.close()


async def test_a_member_that_reports_failed_leaves_a_partial_run(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("board_update", {"status": "failed", "note": "cannot reach the api"}, "u"),
                text("sorry"),
            ]
        }
    )
    runner, store, log = make_runner(tmp_path, provider)
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "call api", "member": "dev"}]))
    assert await runner.run(rid) == "partial"
    assert store.task(rid, "T1").note == "cannot reach the api"
    assert "**Итог:** ЧАСТИЧНО 0/1" in report_of(repo, rid)
    log.close()
    store.close()
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_team_runner.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna_core.team.runner'` (raised from `team_kit`)

- [ ] **Step 4: Implement `team/runner.py`**

```python
"""The team runner: plan once, then members on a board, serial merges, an event-driven lead.

One asyncio loop per run. Each iteration it claims ready tasks for idle
members (an atomic SQL claim), wakes the lead when something changed, and
waits for a member, the lead or a change. A member turn works exactly one
task in its own git worktree; finished code is merged into ``luna/team/<id>``
under one lock and, with a verify command, checked right after.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from luna_core import catalog
from luna_core.assemblies import AssemblyLog
from luna_core.context import build_system_prompt
from luna_core.dynamic import is_writing, resolve_spec
from luna_core.errors import LunaCoreError
from luna_core.events import Cancelled, TurnDone, TurnError
from luna_core.hooks import Hooks
from luna_core.loop import Agent
from luna_core.policy import Rules
from luna_core.providers.base import Provider
from luna_core.rategate import RateGate, gated, shared_gate
from luna_core.roles import ROLES, ModelRoles
from luna_core.sandbox import Sandbox
from luna_core.session import EventLog, Session
from luna_core.subagents import (
    CHILD_FORBIDDEN,
    READ_ONLY,
    WRITE_TOOLS,
    DynamicSettings,
    SubagentSpec,
    child_prompt,
)
from luna_core.team.board import LEAD, TeamMember, TeamRun, TeamStore, TeamTask
from luna_core.team.git import MemberTree, TeamGit
from luna_core.team.inhibit import keep_awake as _keep_awake_default
from luna_core.team.messages import Mailbox
from luna_core.team.permissions import Denied, TeamPolicy
from luna_core.team.plan import (
    PlanError,
    PlanMember,
    Planner,
    TeamPlan,
    parse_member,
    parse_plan,
)
from luna_core.team.report import build_report, write_report
from luna_core.team.settings import TeamSettings
from luna_core.team.tools import TeamContext, lead_tools, member_tools, render_board
from luna_core.tools.base import Tool, args_preview
from luna_core.usage import Usage, cost
from luna_core.workspace import Workspace

MEMBER_PROMPT = """\
Team member: {name} (Luna team {run}). You work on exactly one task from the team \
board, then end your turn. Use board_list to see the board, send_message to ask the \
lead or a teammate, read_messages for answers. Commands outside the team's permission \
profile are refused; do not retry them. Finish with board_update(status, note)."""

LEAD_RUN_PROMPT = """\
You are the Team lead of Luna team {run}. Goal: {goal}
You are woken when the board changes or somebody messages you. Keep the team moving: \
add or split tasks, hire (within the limits) or retire members, reassign stuck tasks, \
answer questions. Only you may broadcast. If nothing needs you, end your turn at once \
without calling tools. You never change files yourself."""


@dataclass
class TeamHost:
    """What the host application gives a team runner."""

    provider: Provider
    model: str
    tools: list[Tool]
    roles: ModelRoles | None = None
    profiles: dict[str, SubagentSpec] = field(default_factory=dict)
    skill_names: Collection[str] = ()
    skill_loader: Callable[[str], str] | None = None
    base_rules: Rules | None = None
    sandbox: Sandbox | None = None
    hooks: Hooks | None = None
    model_overrides: dict | None = None
    journal: AssemblyLog | None = None
    gate: RateGate | None = None
    #: Called once a run ends; returns report lines about subagent profile changes.
    after_run: Callable[[str], Awaitable[list[str]]] | None = None


@dataclass
class _TurnResult:
    ok: bool
    error: str
    text: str
    steps: int
    cost: float
    hit_max_steps: bool
    cancelled: bool


@dataclass
class _RunState:
    """What the loop of one running team keeps in memory."""

    run_id: str
    git: TeamGit
    session: Session
    deadline: float
    budget: float | None
    workers: dict[str, asyncio.Task] = field(default_factory=dict)
    turns: dict[str, tuple[Agent, Session, int]] = field(default_factory=dict)
    lead: asyncio.Task | None = None
    inbox: list[str] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    stopping: str | None = None
    stalled: bool = False
    idle_wakes: int = 0
    last_print: str | None = None
    lead_steps: int = 0
    lead_quiet: bool = False
    orphans: set[str] = field(default_factory=set)
    merge_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TeamRunner:
    """Plans, runs and reports the team runs of one project."""

    def __init__(
        self,
        store: TeamStore,
        log: EventLog,
        host: TeamHost,
        root: str | Path,
        settings: TeamSettings | None = None,
        *,
        clock: Callable[[], float] = time.time,
        tick: float = 0.5,
        keep_awake: Callable[[bool], contextlib.AbstractContextManager] = _keep_awake_default,
    ) -> None:
        self.store = store
        self.mailbox = Mailbox(store)
        self.log = log
        self.host = host
        self.root = Path(root).resolve()
        self.settings = settings or TeamSettings()
        self.clock = clock
        self.tick = tick
        self._keep_awake = keep_awake
        self.gate = host.gate or shared_gate(self.settings.rpm)
        self.gate.listeners.append(self._on_rate)
        self._pool = {t.name: t for t in host.tools}
        self._states: dict[str, _RunState] = {}
        self._seed: dict[str, list[str]] = {}

    def close(self) -> None:
        """Stop listening to the rate gate."""
        with contextlib.suppress(ValueError):
            self.gate.listeners.remove(self._on_rate)

    def _on_rate(self, rpm: float) -> None:
        for run_id in list(self._states):
            self.store.log_event(run_id, "team_rate", rpm=rpm)

    def _run(self, run_id: str) -> TeamRun:
        run = self.store.run(run_id)
        if run is None:
            raise LunaCoreError(f"no team run {run_id}")
        return run

    # --- models and members ----------------------------------------------

    def _model(self, name: str | None) -> tuple[Provider, str]:
        """Provider and model for a role name, a model id or the default; always gated."""
        host = self.host
        if name in ROLES:
            if host.roles is not None:
                provider, model = host.roles.get(name, host.provider, host.model)
            else:
                provider, model = host.provider, host.model
        elif name:
            provider, model = host.provider, name
        else:
            provider, model = host.provider, host.model
        return gated(provider, self.gate), model

    def member_spec(self, member: PlanMember) -> SubagentSpec:
        """Resolve a member against the host's tools, skills, profiles and limits (raises)."""
        if member.profile:
            base = self.host.profiles.get(member.profile)
            if base is None:
                raise PlanError(f"member {member.name}: unknown profile {member.profile!r}")
        else:
            base = SubagentSpec(
                member.name, "team member", member.prompt or "", tools=(*READ_ONLY, *WRITE_TOOLS)
            )
        limits = DynamicSettings(
            enabled=True,
            allow_write=True,
            models=self.settings.models,
            max_steps=self.settings.member_steps,
        )
        spec, _dynamic = resolve_spec(
            base,
            member.assembly(),
            parent_tools=self._pool,
            skills=self.host.skill_names,
            settings=limits,
        )
        return dataclasses.replace(
            spec, name=member.name, max_steps=min(spec.max_steps, self.settings.member_steps)
        )

    # --- lifecycle --------------------------------------------------------

    def create(self, task: str, *, budget: float | None = None) -> str:
        """Start a run in ``planning`` and return its id."""
        session_id = self.log.create(
            str(self.root), title=f"team: {task[:60]}", meta={"kind": "team"}
        )
        run_id = self.store.create_run(
            workdir=str(self.root),
            task=task,
            session_id=session_id,
            settings=self.settings.to_dict(),
            budget=self.settings.budget if budget is None else budget,
        )
        self.log.set_meta(session_id, team=run_id)
        self.store.add_member(run_id, LEAD, {"role": "lead"})
        return run_id

    async def plan(self, run_id: str, *, feedback: str | None = None) -> TeamPlan:
        """Let the lead (role ``strong``) plan read-only; the run then awaits approval."""
        run = self._run(run_id)
        if run.status not in ("planning", "awaiting_plan"):
            raise LunaCoreError(f"team {run_id} is {run.status}; only a new run can be planned")
        session = Session.open(self.log, run.session_id, undo=False)
        provider, model = self._model("strong")
        planner = Planner(
            provider,
            model,
            workspace=Workspace(self.root),
            tools=self.host.tools,
            max_members=self.settings.max_members,
            profiles=self.host.profiles,
            max_steps=self.settings.lead_steps,
            check_member=self.member_spec,
            model_overrides=self.host.model_overrides,
            sandbox=self.host.sandbox,
        )
        previous = TeamPlan.from_dict(run.plan) if run.plan else None
        start = len(session.records())
        before = len(session.messages())
        try:
            plan = await planner.plan(session, run.task, feedback=feedback, previous=previous)
        finally:
            steps = sum(1 for m in session.messages()[before:] if m.role == "assistant")
            spent = self._turn_cost(session, start, model)
            self.store.update_member(run_id, LEAD, add_steps=steps, add_cost=spent)
        self.store.update_run(run_id, status="awaiting_plan", plan=plan.to_dict())
        self.store.log_event(run_id, "team_plan", LEAD, plan=plan.to_dict())
        return plan

    def accept(
        self,
        run_id: str,
        *,
        plan: TeamPlan | None = None,
        allow: Iterable[str] | None = None,
        budget: float | None = None,
    ) -> TeamPlan:
        """Accept the waiting (or an edited) plan: tasks and members go on the board."""
        run = self._run(run_id)
        if run.status != "awaiting_plan" or (plan is None and not run.plan):
            raise LunaCoreError(f"team {run_id} has no plan waiting for approval")
        data = (plan or TeamPlan.from_dict(run.plan)).to_dict()
        if allow is not None:
            data["allow"] = list(allow)
        checked = parse_plan(
            data, max_members=self.settings.max_members, profiles=self.host.profiles
        )
        for member in checked.members:
            self.member_spec(member)
        for task in checked.ordered_tasks():
            self.store.add_task(
                run_id,
                title=task.title,
                kind=task.kind,
                detail=task.detail,
                deps=task.deps,
                assignee=task.member,
                task_id=task.id,
            )
        for member in checked.members:
            self.store.add_member(run_id, member.name, member.to_dict())
            self.store.log_event(run_id, "team_member", member.name, state="hired")
        values: dict = {"status": "running", "plan": checked.to_dict()}
        if budget is not None:
            values["budget"] = budget
        self.store.update_run(run_id, **values)
        return checked

    def decline(self, run_id: str) -> None:
        """Cancel a run before it started; it ends «ОСТАНОВЛЕНО (отмена)»."""
        run = self._run(run_id)
        if run.status not in ("planning", "awaiting_plan"):
            raise LunaCoreError(f"team {run_id} is {run.status}; only a new run can be declined")
        self.store.update_run(
            run_id, status="finished", outcome="stopped", stop_reason="cancel", finished=self.clock()
        )
        report = build_report(self.store, run_id)
        write_report(self.root, run_id, report)
        self.store.log_event(run_id, "team_done", outcome="stopped", headline=report.data["headline"])

    async def run(self, run_id: str) -> str:
        """Run an accepted team to the end and return its outcome."""
        run = self._run(run_id)
        if run.status != "running":
            raise LunaCoreError(f"team {run_id} is {run.status}, not running")
        if run_id in self._states:
            raise LunaCoreError(f"team {run_id} is already running here")
        git = TeamGit(self.root, run_id)
        session = Session.open(self.log, run.session_id, undo=False)
        if run.branch is None:
            base = await asyncio.to_thread(git.start)
            self.store.update_run(run_id, branch=git.branch, base=base, started=self.clock())
        else:
            await asyncio.to_thread(git.ensure)
        st = self._new_state(self._run(run_id), git, session)
        st.inbox.extend(self._seed.pop(run_id, []))
        self._states[run_id] = st
        outcome = "error"
        try:
            with self._keep_awake(not self.settings.allow_sleep) as awake:
                self.store.log_event(run_id, "awake", held=bool(awake))
                outcome = await self._loop(st)
        except asyncio.CancelledError:
            # The process is going away: leave the run "running" for the next start.
            await self._abort(st)
            raise
        except Exception as exc:  # noqa: BLE001 - a crash still ends with a report
            self.store.log_event(run_id, "error", message=f"{type(exc).__name__}: {exc}")
            await self._abort(st)
        finally:
            self._states.pop(run_id, None)
        await self._finish(st, outcome)
        return outcome

    def _new_state(self, run: TeamRun, git: TeamGit, session: Session) -> _RunState:
        started = run.started if run.started is not None else self.clock()
        return _RunState(
            run.id, git, session, deadline=started + self.settings.wall_clock, budget=run.budget
        )

    # --- the loop ---------------------------------------------------------

    async def _loop(self, st: _RunState) -> str:
        while True:
            st.changed.clear()
            if not st.stopping:
                self._dispatch(st)
                self._maybe_wake_lead(st)
            if not st.workers and st.lead is None:
                outcome = self._settled(st)
                if outcome is not None:
                    return outcome
            await self._wait(st)

    async def _wait(self, st: _RunState) -> None:
        waiters = [*st.workers.values(), *([st.lead] if st.lead else [])]
        changed = asyncio.ensure_future(st.changed.wait())
        try:
            await asyncio.wait(
                [changed, *waiters], timeout=self.tick, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            changed.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await changed

    def _settled(self, st: _RunState) -> str | None:
        """The outcome once nothing runs, or None while there is still something to do."""
        tasks = self.store.tasks(st.run_id)
        if tasks and all(t.status == "done" for t in tasks):
            return "done"
        if st.stalled:
            return "stalled"
        if st.stopping:
            return "stopped"
        if self._lead_has_work(st):
            return None
        takers = {
            m.name for m in self.store.members(st.run_id, include_retired=False) if m.name != LEAD
        }
        ready = self.store.ready(st.run_id)
        if any(t.assignee is None or t.assignee in takers for t in ready) and takers:
            return None  # the next dispatch takes them
        stuck = [t for t in ready if t.id not in st.orphans]
        if stuck and not st.lead_quiet:
            st.orphans.update(t.id for t in stuck)
            ids = ", ".join(t.id for t in stuck)
            self._note(st, f"nobody can take {ids} (owner retired or no members): reassign or hire")
            return None
        return "partial"

    def _dispatch(self, st: _RunState) -> None:
        for member in self.store.members(st.run_id, include_retired=False):
            if len(st.workers) >= self.settings.max_concurrent:
                return
            if member.name == LEAD or member.name in st.workers:
                continue
            task = self.store.claim_next(st.run_id, member.name)
            if task is None:
                continue
            self._task_event(st, task, member.name)
            st.workers[member.name] = asyncio.create_task(
                self._work(st, member, task), name=f"team-{st.run_id}-{member.name}"
            )

    # --- members ----------------------------------------------------------

    async def _work(self, st: _RunState, member: TeamMember, task: TeamTask) -> None:
        started = time.monotonic()
        self.store.update_member(st.run_id, member.name, status="working")
        self.store.log_event(st.run_id, "team_member", member.name, state="working", task=task.id)
        try:
            await self._attempt(st, member, task)
        except Exception as exc:  # noqa: BLE001 - a crashed attempt fails its task, not the run
            message = f"{type(exc).__name__}: {exc}"
            self._set_task(st, task.id, "failed", f"crashed: {message}", member.name)
            self.store.log_event(st.run_id, "error", member.name, message=f"{task.id}: {message}")
        finally:
            st.workers.pop(member.name, None)
            self.store.update_member(
                st.run_id, member.name, status="idle", add_busy=time.monotonic() - started
            )
            totals = self.store.member(st.run_id, member.name)
            self.store.log_event(
                st.run_id, "team_member", member.name, state="idle",
                steps=totals.steps, cost=totals.cost,
            )  # fmt: skip
            st.changed.set()

    async def _attempt(self, st: _RunState, member: TeamMember, task: TeamTask) -> None:
        plan_member = PlanMember.from_dict(member.spec)
        spec = self.member_spec(plan_member)
        code = task.kind == "code"
        tree: MemberTree | None = None
        if code:
            merge = None
            if task.merge_of:
                source = self.store.task(st.run_id, task.merge_of)
                merge = source.branch if source else None
            tree = await asyncio.to_thread(
                st.git.member_tree, task.id, task.attempts, branch=task.branch, merge=merge
            )
            self.store.update_task(st.run_id, task.id, branch=tree.branch, worktree=str(tree.path))
            workspace = Workspace(tree.path)
        else:
            workspace = Workspace(st.git.integration)
        ctx = TeamContext(
            self.store,
            self.mailbox,
            st.run_id,
            member.name,
            task_id=task.id,
            stopping=lambda: st.stopping is not None,
            on_change=st.changed.set,
        )
        session = self._member_session(st, member.name, task, workspace)
        agent = self._member_agent(st, spec, workspace, ctx, session, code=code)
        try:
            result = await self._drive(st, member.name, agent, session, self._task_text(task))
        except Exception:
            if tree is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(st.git.salvage, tree.path, tree.branch)
                self.store.update_task(st.run_id, task.id, worktree=None)
            raise
        self.store.update_member(st.run_id, member.name, add_steps=result.steps, add_cost=result.cost)
        self._journal(st, plan_member, spec, session, task, result)
        status, note = self._outcome(ctx, task, spec, result)
        branch = None
        if tree is not None:
            stat, commits = await asyncio.to_thread(
                st.git.finish, tree, f"luna team {st.run_id} {task.id}: {task.title[:60]}"
            )
            branch = tree.branch if stat else None
            task = self.store.update_task(
                st.run_id, task.id, worktree=None, branch=branch, commits=[*task.commits, *commits]
            )
        if st.stopping and status != "done":
            kept = f"; work kept on {branch}" if branch else ""
            self._set_task(st, task.id, "pending", f"stopped ({st.stopping}){kept}", member.name)
            return
        if status != "done":
            self._set_task(st, task.id, status, note, member.name)
            return
        if not code:
            self._write_note(st, task, note or result.text)
            self._set_task(st, task.id, "done", note or result.text[:300], member.name)
            return
        if branch is None:
            if task.merge_of:
                self._retry_or_fail(st, task, "the conflict was not resolved (no changes)", member.name)
                return
            self._set_task(st, task.id, "done", note or "no changes", member.name)
            return
        await self._integrate(st, task, branch, note, member.name)

    @staticmethod
    def _outcome(
        ctx: TeamContext, task: TeamTask, spec: SubagentSpec, result: _TurnResult
    ) -> tuple[str, str]:
        reported = ctx.reports.get(task.id)
        if reported:
            return reported
        if result.hit_max_steps:
            return "failed", f"ran out of steps ({spec.max_steps})"
        if not result.ok:
            return "failed", result.error or "the member stopped without finishing"
        return "done", result.text[-500:]

    async def _integrate(
        self, st: _RunState, task: TeamTask, branch: str, note: str, member: str
    ) -> None:
        """Merge a finished task branch (one at a time), verify, and settle the task."""
        async with st.merge_lock:
            message = f"luna team {st.run_id}: {task.id} {task.title[:60]}"
            result = await asyncio.to_thread(st.git.merge, branch, message)
            if result.status == "markers":
                self.store.log_event(st.run_id, "markers", member, task=task.id, files=result.detail)
                self._retry_or_fail(st, task, f"conflict markers left in {result.detail}", member)
                return
            if result.status == "conflict":
                fix = self.store.add_task(
                    st.run_id,
                    title=f"resolve merge conflict of {task.id}",
                    kind="code",
                    detail=(
                        f"Merging {branch} into the team branch conflicts. Your worktree has "
                        "that merge started: resolve every conflicted file (no <<<<<<< or "
                        ">>>>>>> lines left) keeping both sides' intent.\n\n"
                        f"{result.detail[-1500:]}"
                    ),
                    merge_of=task.id,
                )
                self.store.log_event(st.run_id, "conflict", member, task=task.id, fix=fix.id)
                self._task_event(st, fix, "")
                self._set_task(st, task.id, "blocked", f"merge conflict → {fix.id}", member)
                return
            if result.status == "merged" and self.settings.verify_command:
                ok, output = await asyncio.to_thread(
                    st.git.verify, self.settings.verify_command, self.settings.verify_timeout
                )
                if not ok:
                    await asyncio.to_thread(st.git.undo_merge, result.before)
                    self.store.log_event(
                        st.run_id, "verify_failed", member, task=task.id,
                        attempt=task.attempts, output=output[-1500:],
                    )  # fmt: skip
                    self._retry_or_fail(
                        st, task, f"verify failed after merging:\n{output[-1500:]}", member
                    )
                    return
        await asyncio.to_thread(st.git.delete_branch, branch)
        self.store.update_task(st.run_id, task.id, branch=None)
        self.store.log_event(st.run_id, "merge", member, task=task.id, commit=result.commit)
        self._set_task(st, task.id, "done", note, member)
        if task.merge_of:
            source = self.store.task(st.run_id, task.merge_of)
            if source is not None and source.status == "blocked":
                if source.branch:
                    await asyncio.to_thread(st.git.delete_branch, source.branch)
                self.store.update_task(st.run_id, source.id, branch=None)
                self._set_task(st, source.id, "done", f"merged via {task.id}", "")

    def _retry_or_fail(self, st: _RunState, task: TeamTask, note: str, member: str) -> None:
        status = "failed" if task.attempts >= self.settings.max_attempts else "pending"
        self._set_task(st, task.id, status, note, member)

    def _set_task(self, st: _RunState, task_id: str, status: str, note: str, member: str) -> None:
        task = self.store.update_task(st.run_id, task_id, status=status, note=note)
        self._task_event(st, task, member)
        who = f" ({member})" if member else ""
        short = " ".join(note.split())[:300]
        self._note(st, f"{task_id} {status}{who}: {short}" if short else f"{task_id} {status}{who}")

    def _task_event(self, st: _RunState, task: TeamTask, member: str) -> None:
        self.store.log_event(
            st.run_id, "team_task", member, task=task.id, status=task.status,
            note=task.note[:300], assignee=task.assignee,
        )  # fmt: skip

    def _note(self, st: _RunState, line: str) -> None:
        """Tell the lead about something at its next wake."""
        st.inbox.append(line)
        st.changed.set()

    @staticmethod
    def _task_text(task: TeamTask) -> str:
        lines = [f"Task {task.id}: {task.title}"]
        if task.detail:
            lines += ["", task.detail]
        if task.kind == "code":
            lines += ["", "Your working directory is your own git worktree of the team branch. "
                      "Change files there; Luna commits your work when your turn ends."]  # fmt: skip
        else:
            lines += ["", "This is a research task: read, do not change files. Your final "
                      "note becomes the task's notes file."]  # fmt: skip
        if task.attempts > 1 and task.note:
            lines += ["", f"Earlier attempt: {task.note}"]
        lines += ["", 'When you are done call board_update(status="done", note="<what you did>"); '
                  'if you cannot finish, report "blocked" or "failed" with the reason.']  # fmt: skip
        return "\n".join(lines)

    def _member_session(
        self, st: _RunState, name: str, task: TeamTask, workspace: Workspace
    ) -> Session:
        sid = self.log.create(
            str(workspace.root),
            title=f"{name}: {task.id} {task.title[:50]}",
            parent_id=st.session.id,
            meta={"kind": "team-member", "team": st.run_id, "member": name, "task": task.id,
                  "attempt": task.attempts},
        )  # fmt: skip
        return Session(self.log, sid, workdir=str(workspace.root), undo=False)

    def _member_agent(
        self,
        st: _RunState,
        spec: SubagentSpec,
        workspace: Workspace,
        ctx: TeamContext,
        session: Session,
        *,
        code: bool,
    ) -> Agent:
        names = [n for n in spec.tools if n in self._pool and n not in CHILD_FORBIDDEN]
        if not code:
            names = [n for n in names if not is_writing(n, self._pool)]
        skills: list[tuple[str, str]] = []
        if spec.skills and self.host.skill_loader is not None:
            skills = [(n, self.host.skill_loader(n)) for n in spec.skills]
        provider, model = self._model(spec.model)
        run = self._run(st.run_id)
        prompt = MEMBER_PROMPT.format(name=spec.name, run=st.run_id)
        policy = TeamPolicy(
            (run.plan or {}).get("allow", ()),
            deny=self._deny_rules(),
            workspace=workspace,
            member=spec.name,
            on_denied=self._denied(st),
            file_writes=code,
        )
        return Agent(
            provider,
            model,
            tools=[*(self._pool[n] for n in names), *member_tools(ctx)],
            system=build_system_prompt(
                f"{prompt}\n\n{child_prompt(spec, skills)}", root=workspace.root, memory=[]
            ),
            policy=policy,
            workspace=workspace,
            sandbox=self.host.sandbox,
            hooks=self.host.hooks,
            max_steps=spec.max_steps,
            max_cost=self._max_cost(st, session),
            model_overrides=self.host.model_overrides,
            name=spec.name,
        )

    def _turn_cost(self, session: Session, start: int, model: str) -> float:
        """USD of the model calls recorded after record ``start`` (also of failed turns)."""
        total = Usage()
        for rec in session.records()[start:]:
            if rec.type == "assistant" and rec.payload.get("usage"):
                total = total + Usage.from_dict(rec.payload["usage"])
        return cost(total, catalog.lookup(model, self.host.model_overrides)) or 0.0

    async def _drive(
        self, st: _RunState, name: str, agent: Agent, session: Session, text: str
    ) -> _TurnResult:
        """Run one turn to its end; ``st.turns`` lets a soft stop shorten it."""
        start = len(session.records())
        before = len(session.messages())
        ok, error, hit, cancelled = True, "", False, False
        st.turns[name] = (agent, session, before)
        try:
            async with contextlib.aclosing(agent.run(session, text)) as events:
                async for event in events:
                    if isinstance(event, TurnDone):
                        hit = event.stop_reason == "max_steps"
                    elif isinstance(event, TurnError):
                        ok, error = False, event.message
                        if event.kind != "budget":
                            self.store.log_event(
                                st.run_id, "provider_error", name, message=event.message[:300]
                            )
                    elif isinstance(event, Cancelled):
                        ok, error, cancelled = False, "cancelled", True
        finally:
            st.turns.pop(name, None)
        added = session.messages()[before:]
        answers = [m for m in added if m.role == "assistant" and m.text]
        return _TurnResult(
            ok,
            error,
            answers[-1].text if answers else "",
            sum(1 for m in added if m.role == "assistant"),
            self._turn_cost(session, start, agent.model),
            hit,
            cancelled,
        )

    def _journal(
        self,
        st: _RunState,
        member: PlanMember,
        spec: SubagentSpec,
        session: Session,
        task: TeamTask,
        result: _TurnResult,
    ) -> None:
        """Feed the assembly journal (spec part 1b) so good members become profiles."""
        if self.host.journal is None:
            return
        with contextlib.suppress(Exception):
            self.host.journal.record(
                workdir=str(self.root),
                parent_session=st.session.id,
                child_session=session.id,
                base_profile=member.profile or "team",
                dynamic=bool(member.assembly()),
                spec=spec.to_dict(),
                task=f"{task.title}\n{task.detail}".strip(),
                ok=result.ok,
                cancelled=result.cancelled,
                hit_max_steps=result.hit_max_steps,
                steps=result.steps,
                cost=result.cost,
                mutating=[n for n in spec.tools if is_writing(n, self._pool)],
            )

    def _write_note(self, st: _RunState, task: TeamTask, text: str) -> None:
        path = self.root / ".luna" / "teams" / st.run_id / "notes" / f"{task.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {task.id}: {task.title}\n\n{text.strip()}\n", encoding="utf-8")

    def _deny_rules(self) -> list[str]:
        return list(self.host.base_rules.deny) if self.host.base_rules is not None else []

    def _denied(self, st: _RunState) -> Denied:
        def record(member: str, tool: str, args: dict, reason: str) -> None:
            preview = str(args.get("command") or "") or args_preview(args, 120)
            self.store.log_event(
                st.run_id, "team_denied", member, tool=tool, preview=preview, reason=reason
            )

        return record

    def _spent(self, st: _RunState) -> float:
        """Everything the run's members and lead have cost so far."""
        return sum(m.cost for m in self.store.members(st.run_id))

    def _max_cost(self, st: _RunState, session: Session) -> float | None:
        if st.budget is None:
            return None
        return session.cost() + max(0.0, st.budget - self._spent(st))

    # --- the lead ---------------------------------------------------------

    def _lead_has_work(self, st: _RunState) -> bool:
        if st.lead_quiet or st.stalled:
            return False
        return bool(st.inbox) or self.mailbox.count_unread(st.run_id, LEAD) > 0

    def _maybe_wake_lead(self, st: _RunState) -> None:
        if st.lead is not None or not self._lead_has_work(st):
            return
        lines, st.inbox = st.inbox, []
        st.lead = asyncio.create_task(self._wake_lead(st, lines), name=f"team-{st.run_id}-lead")

    async def _wake_lead(self, st: _RunState, lines: list[str]) -> None:
        try:
            ctx = TeamContext(
                self.store,
                self.mailbox,
                st.run_id,
                LEAD,
                stopping=lambda: st.stopping is not None,
                hire=lambda args: self._hire(st.run_id, args),
                on_change=st.changed.set,
            )
            agent = self._lead_agent(st, ctx, self.settings.lead_steps - st.lead_steps)
            result = await self._drive(st, LEAD, agent, st.session, self._wake_text(st, lines))
            st.lead_steps += result.steps
            self.store.update_member(st.run_id, LEAD, add_steps=result.steps, add_cost=result.cost)
            if st.lead_steps >= self.settings.lead_steps:
                st.lead_quiet = True
                self.store.log_event(st.run_id, "lead_out_of_steps", LEAD)
        except Exception as exc:  # noqa: BLE001 - a broken lead must not take the run down
            st.lead_quiet = True
            self.store.log_event(
                st.run_id, "error", LEAD, message=f"lead: {type(exc).__name__}: {exc}"
            )
        finally:
            st.lead = None
            st.changed.set()

    def _wake_text(self, st: _RunState, lines: list[str]) -> str:
        run = self._run(st.run_id)
        goal = (run.plan or {}).get("goal", run.task)
        parts = [f"Team {st.run_id}. Goal: {goal}", "", "Board:", render_board(self.store, st.run_id)]
        if lines:
            parts += ["", "Since you last looked:", *(f"- {line}" for line in lines)]
        messages = self.mailbox.unread(st.run_id, LEAD)
        if messages:
            parts += ["", "Messages to you:", *(f"- from {m.sender}: {m.text}" for m in messages)]
        parts += ["", "Act with your tools if the team needs you; otherwise end your turn now."]
        return "\n".join(parts)

    def _lead_agent(self, st: _RunState, ctx: TeamContext, max_steps: int) -> Agent:
        run = self._run(st.run_id)
        workspace = Workspace(st.git.integration)
        provider, model = self._model("strong")
        read_only = [self._pool[n] for n in READ_ONLY if n in self._pool]
        prompt = LEAD_RUN_PROMPT.format(run=st.run_id, goal=(run.plan or {}).get("goal", run.task))
        return Agent(
            provider,
            model,
            tools=[*read_only, *lead_tools(ctx)],
            system=build_system_prompt(prompt, root=workspace.root, memory=[]),
            policy=TeamPolicy(
                (), deny=self._deny_rules(), workspace=workspace, member=LEAD,
                on_denied=self._denied(st),
            ),  # fmt: skip
            workspace=workspace,
            sandbox=self.host.sandbox,
            max_steps=max(1, max_steps),
            max_cost=self._max_cost(st, st.session),
            model_overrides=self.host.model_overrides,
            name=LEAD,
        )

    def _hire(self, run_id: str, args: dict) -> str:
        """The lead's ``hire``: within ``max_members`` and only a valid assembly."""
        hired = [m for m in self.store.members(run_id) if m.name != LEAD]
        if len(hired) >= self.settings.max_members:
            raise ValueError(f"member limit reached ({self.settings.max_members} per run)")
        member = parse_member(args, self.host.profiles)
        self.member_spec(member)
        self.store.add_member(run_id, member.name, member.to_dict())
        self.store.log_event(run_id, "team_member", member.name, state="hired")
        self.store.log_event(run_id, "decision", LEAD, text=f"hired {member.name}")
        return f"hired {member.name}"

    # --- the end ----------------------------------------------------------

    @staticmethod
    async def _abort(st: _RunState) -> None:
        tasks = [*st.workers.values(), *([st.lead] if st.lead else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _finish(self, st: _RunState, outcome: str) -> None:
        run = self._run(st.run_id)
        self.store.update_run(
            st.run_id,
            status="finished",
            outcome=outcome,
            stop_reason=st.stopping if outcome == "stopped" else None,
            finished=self.clock(),
        )
        diffstat = ""
        if run.base:
            with contextlib.suppress(Exception):
                diffstat = await asyncio.to_thread(st.git.diffstat, run.base)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(st.git.close)
        changes: list[str] = []
        if self.host.after_run is not None:
            with contextlib.suppress(Exception):
                changes = list(await self.host.after_run(st.run_id))
        report = build_report(self.store, st.run_id, diffstat=diffstat, profile_changes=changes)
        path = write_report(self.root, st.run_id, report)
        self.store.log_event(
            st.run_id, "team_done", outcome=outcome, headline=report.data["headline"],
            report=str(path),
        )  # fmt: skip
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest packages/luna-core/tests/test_team_runner.py -q`
Expected: PASS (6 tests). Diagnose failures with `superpowers:systematic-debugging` before changing tests.
- If the conflict test ends without a `conflict` event, the two attempts did not overlap. Raise `delay` to 0.1 before suspecting the code.
- A hang is usually `_settled` returning `None` forever. Print `store.tasks(rid)` and `st.inbox` from a breakpoint in `_loop`.

- [ ] **Step 6: Lint**

Run: `uv run ruff check packages/luna-core && uv run ruff format --check packages/luna-core`
Expected: clean. Wrap any line ruff reports as too long without changing behaviour.

- [ ] **Step 7: Commit**

```bash
git add packages/luna-core/src/luna_core/team/runner.py packages/luna-core/tests/team_kit.py packages/luna-core/tests/test_team_runner.py
git commit -m "feat(team): runner with members in worktrees, serial merges and an event-driven lead"
```

---
### Task 11: Limits — soft stop on time and budget, cancel, stall detector

**Files:**
- Modify: `packages/luna-core/src/luna_core/team/runner.py`
- Test: `packages/luna-core/tests/test_team_limits.py`

**Interfaces:**
- Consumes: `TeamRunner` and `_RunState` from Task 10.
- Produces:
  - `TeamRunner.cancel(run_id) -> bool`: a soft stop of a running run, or `decline` of a run that has not started;
  - `TeamRunner._check_limits(st)`, `_stop(st, reason)`, `_stall_check(st) -> "" | "warn" | "stop"`;
  - stop reasons `"time"`, `"budget"`, `"cancel"`, `"stall"`;
  - events `soft_stop`, `stall_warn`, `stalled`.

- [ ] **Step 1: Write the failing tests `packages/luna-core/tests/test_team_limits.py`**

```python
"""Team limits: concurrency, steps, members, budget, wall clock, cancel, stall, sleep."""

import contextlib

import pytest
from luna_core.errors import LunaCoreError
from luna_core.session import Session
from luna_core.team.git import TeamGit
from luna_core.team.settings import TeamSettings
from team_kit import (
    TeamProvider,
    call,
    done,
    git,
    make_repo,
    make_runner,
    plan_dict,
    ready_run,
    report_of,
    text,
)

TWO_TASKS = [
    {"id": "T1", "title": "first", "member": "dev"},
    {"id": "T2", "title": "second", "deps": ["T1"], "member": "dev"},
]


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


async def test_no_more_than_max_concurrent_members_work_at_once(repo, tmp_path):
    names = ["amy", "bob", "cat"]
    provider = TeamProvider(
        {
            n: [call("write_file", {"file_path": f"{n}.txt", "content": n}, "w"), done(n), text("ok")]
            for n in names
        },
        delay=0.05,
    )
    runner, store, log = make_runner(tmp_path, provider, settings=TeamSettings(max_concurrent=2))
    rid = ready_run(
        runner,
        store,
        plan_dict(
            [{"id": f"T{i}", "title": f"file {n}", "member": n} for i, n in enumerate(names, 1)],
            members=[{"name": n, "prompt": "You write."} for n in names],
        ),
    )
    assert await runner.run(rid) == "done"
    busy = peak = 0
    for event in store.events(rid, kinds=["team_member"]):
        if event.data.get("state") == "working":
            busy += 1
            peak = max(peak, busy)
        elif event.data.get("state") == "idle":
            busy -= 1
    assert peak == 2
    log.close()
    store.close()


async def test_member_steps_are_capped(repo, tmp_path):
    reads = [call("read_file", {"file_path": "a.txt"}, f"r{i}") for i in range(5)]
    provider = TeamProvider({"dev": reads})
    runner, store, log = make_runner(tmp_path, provider, settings=TeamSettings(member_steps=2))
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "read", "member": "dev"}]))
    assert await runner.run(rid) == "partial"
    task = store.task(rid, "T1")
    assert (task.status, task.note) == ("failed", "ran out of steps (2)")
    assert len(provider.requests["dev"]) == 2
    log.close()
    store.close()


async def test_the_lead_goes_quiet_when_its_steps_run_out(repo, tmp_path):
    provider = TeamProvider({"dev": [done("one"), text("ok"), done("two"), text("ok")]})
    runner, store, log = make_runner(tmp_path, provider, settings=TeamSettings(lead_steps=1))
    rid = ready_run(runner, store, plan_dict(TWO_TASKS))
    assert await runner.run(rid) == "done"
    assert len(provider.requests["lead"]) == 1
    assert len(store.events(rid, kinds=["lead_out_of_steps"])) == 1
    log.close()
    store.close()


def test_hire_respects_the_member_limit_and_checks_the_assembly(repo, tmp_path):
    runner, store, log = make_runner(
        tmp_path, TeamProvider({}), settings=TeamSettings(max_members=2)
    )
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "x", "member": "dev"}]))
    with pytest.raises(LunaCoreError, match="unknown tools"):
        runner._hire(rid, {"name": "qa", "prompt": "test", "tools": ["nuke"]})
    assert runner._hire(rid, {"name": "qa", "prompt": "test"}) == "hired qa"
    with pytest.raises(ValueError, match="member limit"):
        runner._hire(rid, {"name": "qb", "prompt": "test"})
    log.close()
    store.close()


async def test_budget_stops_softly(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
            ]
        }
    )
    runner, store, log = make_runner(
        tmp_path,
        provider,
        settings=TeamSettings(budget=20.0, models=("member-model",)),
        overrides={"member-model": {"input": 1_000_000, "output": 1_000_000}},
    )
    rid = ready_run(
        runner,
        store,
        plan_dict(TWO_TASKS, members=[{"name": "dev", "prompt": "You write.", "model": "member-model"}]),
    )
    assert await runner.run(rid) == "stopped"
    assert store.run(rid).stop_reason == "budget"
    assert store.task(rid, "T1").status == "done" and store.task(rid, "T2").status == "pending"
    assert git(repo, "show", f"luna/team/{rid}:b.txt") == "b\n"
    assert len(provider.requests["dev"]) == 2  # the third call was refused by max_cost
    assert store.member(rid, "dev").cost == pytest.approx(30.0)
    assert "**Итог:** ОСТАНОВЛЕНО (бюджет)" in report_of(repo, rid)
    log.close()
    store.close()


async def test_wall_clock_stops_softly_and_grants_one_more_step(repo, tmp_path):
    clock = Clock()

    async def jump(who, request):
        if who == "dev" and len(provider.requests["dev"]) == 1:
            clock.now += 10_000

    writes = [call("write_file", {"file_path": f"f{i}.txt", "content": "x"}, f"w{i}") for i in range(1, 5)]
    provider = TeamProvider({"dev": writes}, delay=0.2, before=jump)
    runner, store, log = make_runner(
        tmp_path, provider, settings=TeamSettings(wall_clock=100), clock=clock
    )
    rid = ready_run(runner, store, plan_dict(TWO_TASKS))
    assert await runner.run(rid) == "stopped"
    assert store.run(rid).stop_reason == "time"
    assert len(provider.requests["dev"]) == 2
    first = store.task(rid, "T1")
    assert first.status == "pending" and first.note.startswith("stopped (time)") and first.branch
    assert git(repo, "show", f"{first.branch}:f2.txt") == "x"
    assert store.task(rid, "T2").status == "pending"
    assert "**Итог:** ОСТАНОВЛЕНО (время)" in report_of(repo, rid)
    log.close()
    store.close()


async def test_cancel_stops_softly(repo, tmp_path):
    holder = {}

    async def cancel_on_first(who, request):
        if who == "dev" and len(provider.requests["dev"]) == 1:
            assert holder["runner"].cancel(holder["rid"]) is True

    provider = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
            ]
        },
        before=cancel_on_first,
    )
    runner, store, log = make_runner(tmp_path, provider)
    rid = ready_run(runner, store, plan_dict(TWO_TASKS))
    holder.update(runner=runner, rid=rid)
    assert await runner.run(rid) == "stopped"
    assert store.run(rid).stop_reason == "cancel"
    assert store.task(rid, "T1").status == "done" and store.task(rid, "T2").status == "pending"
    assert runner.cancel(rid) is False  # finished runs cannot be cancelled
    log.close()
    store.close()


def test_stall_detector_warns_then_stops(repo, tmp_path):
    runner, store, log = make_runner(tmp_path, TeamProvider({}))
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "x", "member": "dev"}]))
    run = store.run(rid)
    st = runner._new_state(run, TeamGit(repo, rid), Session.open(log, run.session_id, undo=False))
    assert [runner._stall_check(st) for _ in range(4)] == ["", "", "", "warn"]
    store.update_task(rid, "T1", note="still thinking")  # notes do not count as progress
    assert runner._stall_check(st) == ""  # idle 4: past the warning, not yet the stop
    store.update_task(rid, "T1", status="blocked")  # a real change resets the count
    assert [runner._stall_check(st) for _ in range(7)] == ["", "", "", "warn", "", "", "stop"]
    assert st.stalled and st.stopping == "stall"
    assert runner._settled(st) == "stalled"
    kinds = [e.kind for e in store.events(rid, kinds=["stall_warn", "stalled", "soft_stop"])]
    assert kinds == ["stall_warn", "stall_warn", "stalled", "soft_stop"]
    log.close()
    store.close()


@pytest.mark.parametrize(("allow", "held"), [(False, True), (True, False)])
async def test_sleep_is_held_off_unless_allowed(tmp_path, allow, held):
    make_repo(tmp_path / "repo")
    seen = []

    @contextlib.contextmanager
    def fake(enabled):
        seen.append(enabled)
        yield enabled

    provider = TeamProvider({"dev": [done("ok"), text("ok")]})
    runner, store, log = make_runner(
        tmp_path, provider, settings=TeamSettings(allow_sleep=allow), keep_awake=fake
    )
    rid = ready_run(runner, store, plan_dict([{"id": "T1", "title": "x", "member": "dev"}]))
    await runner.run(rid)
    assert seen == [held]
    assert store.events(rid, kinds=["awake"])[0].data == {"held": held}
    log.close()
    store.close()
```

The stall test counts the board fingerprint, which ignores notes. After the first four checks `idle_wakes` is 3. The note-only update leaves the fingerprint unchanged, so idle becomes 4 and the check returns `""`. The status change makes the next check see a new board, so idle drops to 0. The seven checks after it go 0, 1, 2, 3 (`warn`), 4, 5, 6 (`stop`).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_team_limits.py -q`
Expected:
- FAIL: budget, wall clock and cancel (`AttributeError: 'TeamRunner' object has no attribute 'cancel'`, or the run ends `done`/`partial`), and the stall test (`_stall_check` missing);
- PASS already: concurrency, steps, lead steps, hire and sleep (Task 10 enforces them). Keep them: they pin those limits.

- [ ] **Step 3: Add the limit code to `runner.py`**

After `LEAD_RUN_PROMPT` add:

```python
STALL_NOTE = (
    "The board has not changed for several wakes. Stop discussing: assign, split, reassign "
    "or retire now, or end your turn and let the run finish."
)
```

In `_loop`, check the limits first in every iteration:

```python
    async def _loop(self, st: _RunState) -> str:
        while True:
            st.changed.clear()
            self._check_limits(st)
            if not st.stopping:
                self._dispatch(st)
                self._maybe_wake_lead(st)
            if not st.workers and st.lead is None:
                outcome = self._settled(st)
                if outcome is not None:
                    return outcome
            await self._wait(st)
```

In `_attempt`, right after `self._journal(st, plan_member, spec, session, task, result)`, add:

```python
        self._check_limits(st)
```

Replace `_maybe_wake_lead` with:

```python
    def _maybe_wake_lead(self, st: _RunState) -> None:
        if st.lead is not None or not self._lead_has_work(st):
            return
        verdict = self._stall_check(st)
        if verdict == "stop":
            return
        lines, st.inbox = st.inbox, []
        if verdict == "warn":
            lines.append(STALL_NOTE)
        st.lead = asyncio.create_task(self._wake_lead(st, lines), name=f"team-{st.run_id}-lead")
```

Add these methods to `TeamRunner`. Put `cancel` after `decline`, and the other three in a new section `# --- limits ---` before `# --- the lead ---`:

```python
    def cancel(self, run_id: str) -> bool:
        """Stop a running team softly, or decline one that has not started."""
        st = self._states.get(run_id)
        if st is not None:
            self._stop(st, "cancel")
            return True
        run = self.store.run(run_id)
        if run is not None and run.status in ("planning", "awaiting_plan"):
            self.decline(run_id)
            return True
        return False

    # --- limits -----------------------------------------------------------

    def _check_limits(self, st: _RunState) -> None:
        """Soft-stop the run once the wall clock or the budget runs out."""
        if st.stopping:
            return
        if self.clock() >= st.deadline:
            self._stop(st, "time")
        elif st.budget is not None and self._spent(st) >= st.budget:
            self._stop(st, "budget")

    def _stop(self, st: _RunState, reason: str) -> None:
        """No new tasks; running turns get the step in flight plus one more to save work."""
        if st.stopping:
            return
        st.stopping = reason
        self.store.log_event(st.run_id, "soft_stop", reason=reason)
        for agent, session, before in list(st.turns.values()):
            done = sum(1 for m in session.messages()[before:] if m.role == "assistant")
            agent.max_steps = min(agent.max_steps, done + 2)
        st.changed.set()

    def _stall_check(self, st: _RunState) -> str:
        """Called at every lead wake: ``""``, ``"warn"`` or ``"stop"`` (the run then stalls)."""
        mark = self.store.fingerprint(st.run_id)
        st.idle_wakes = st.idle_wakes + 1 if mark == st.last_print else 0
        st.last_print = mark
        limit = self.settings.stall_wakes
        if st.idle_wakes >= 2 * limit:
            st.stalled = True
            self.store.log_event(st.run_id, "stalled", LEAD)
            self._stop(st, "stall")
            return "stop"
        if st.idle_wakes == limit:
            self.store.log_event(st.run_id, "stall_warn", LEAD)
            return "warn"
        return ""
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/luna-core/tests/test_team_limits.py packages/luna-core/tests/test_team_runner.py -q`
Expected: PASS.

If the wall-clock test sees 3 dev requests, the soft stop landed after the first request had finished. Check that `_check_limits` runs at the top of `_loop` and that `tick=0.01` reaches the runner. Do not loosen the assertion.

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/team/runner.py packages/luna-core/tests/test_team_limits.py
git commit -m "feat(team): soft stop on time, budget and cancel; stall detector"
```

---

### Task 12: Planning, restart recovery and resume

**Files:**
- Modify: `packages/luna-core/src/luna_core/team/runner.py`
- Test: `packages/luna-core/tests/test_team_lifecycle.py`

**Interfaces:**
- Consumes: `TeamRunner` (Tasks 10–11), `TeamGitError` (Task 6).
- Produces:
  - `TeamRunner.resume(run_id) -> outcome`;
  - `TeamRunner.interrupted() -> list[str]`, the runs of this project left `running` and not run in this process (plan 2b calls it when the server starts);
  - `run()` recovers a run left `running`: tasks in progress go back to `pending` with the note «прервано в HH:MM, продолжено», and the event `interrupted` is logged;
  - a run that cannot start (no git repository) ends `ОШИБКА` with a report and re-raises.

- [ ] **Step 1: Write the failing tests `packages/luna-core/tests/test_team_lifecycle.py`**

```python
"""Team lifecycle: plan → accept / decline, restart recovery, resume."""

import asyncio
from pathlib import Path

import pytest
from luna_core.errors import LunaCoreError
from luna_core.team.git import TeamGitError
from team_kit import (
    TeamProvider,
    call,
    done,
    git,
    make_repo,
    make_runner,
    plan_dict,
    ready_run,
    report_of,
    text,
)

ONE = [{"id": "T1", "title": "write b", "kind": "code", "member": "dev"}]


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


async def test_plan_edit_accept_and_decline(repo, tmp_path):
    first = plan_dict([{"id": "T1", "title": "look", "kind": "research", "member": "dev"}])
    second = plan_dict(
        [
            {"id": "T1", "title": "look", "kind": "research", "member": "dev"},
            {"id": "T2", "title": "change", "deps": ["T1"], "member": "dev"},
        ]
    )
    provider = TeamProvider(
        {
            "lead": [
                call("submit_plan", first, "p1"),
                text("planned"),
                call("submit_plan", second, "p2"),
                text("replanned"),
            ]
        }
    )
    runner, store, log = make_runner(tmp_path, provider)
    rid = runner.create("do x")
    plan = await runner.plan(rid)
    assert len(plan.tasks) == 1 and store.run(rid).status == "awaiting_plan"
    assert len(store.events(rid, kinds=["team_plan"])) == 1
    plan = await runner.plan(rid, feedback="split it")
    assert len(plan.tasks) == 2
    assert "split it" in provider.requests["lead"][2].messages[-1].text
    assert provider.requests["lead"][0].model == "lead-model"
    runner.accept(rid, budget=5.0)
    run = store.run(rid)
    assert (run.status, run.budget) == ("running", 5.0)
    assert [t.id for t in store.tasks(rid)] == ["T1", "T2"]
    assert {m.name for m in store.members(rid)} == {"lead", "dev"}
    assert store.member(rid, "lead").steps == 4
    with pytest.raises(LunaCoreError):
        await runner.plan(rid)
    other = runner.create("never mind")
    assert runner.cancel(other) is True
    assert store.run(other).outcome == "stopped"
    assert "**Итог:** ОСТАНОВЛЕНО (отмена)" in report_of(repo, other)
    log.close()
    store.close()


async def test_run_survives_a_restart(repo, tmp_path):
    never = asyncio.Event()

    async def hang(who, request):
        if who == "dev":
            await never.wait()  # the "server" dies while this request is in flight

    first = TeamProvider({"dev": [text("unused")]}, before=hang)
    runner, store, log = make_runner(tmp_path, first)
    rid = ready_run(runner, store, plan_dict(ONE))
    running = asyncio.create_task(runner.run(rid))
    for _ in range(500):
        if first.requests.get("dev"):
            break
        await asyncio.sleep(0.01)
    task = store.task(rid, "T1")
    assert task.status == "in_progress" and Path(task.worktree).exists()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert store.run(rid).status == "running"
    assert store.task(rid, "T1").status == "in_progress"
    log.close()
    store.close()

    second = TeamProvider(
        {
            "dev": [
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
            ]
        }
    )
    runner2, store2, log2 = make_runner(tmp_path, second)
    assert runner2.interrupted() == [rid]
    assert await runner2.run(rid) == "done"
    assert git(repo, "show", f"luna/team/{rid}:b.txt") == "b\n"
    assert "↻ прервано в" in report_of(repo, rid)
    assert "Earlier attempt: прервано в" in second.requests["dev"][0].messages[0].text
    assert store2.task(rid, "T1").attempts == 2
    assert list((repo / ".luna" / "worktrees").glob("team-*")) == []
    assert runner2.interrupted() == []
    log2.close()
    store2.close()


async def test_resume_continues_a_partial_run(repo, tmp_path):
    provider = TeamProvider(
        {
            "dev": [
                call("board_update", {"status": "failed", "note": "need the api key"}, "u1"),
                text("stuck"),
                call("write_file", {"file_path": "b.txt", "content": "b\n"}, "w1"),
                done("wrote b"),
                text("done"),
            ]
        }
    )
    runner, store, log = make_runner(tmp_path, provider)
    rid = ready_run(runner, store, plan_dict(ONE))
    assert await runner.run(rid) == "partial"
    assert await runner.resume(rid) == "done"
    wakes = [r.messages[-1].text for r in provider.requests["lead"]]
    assert any("Resumed by the user" in w and "ЧАСТИЧНО 0/1" in w for w in wakes)
    task = store.task(rid, "T1")
    assert (task.status, task.attempts) == ("done", 1)
    assert store.events(rid, kinds=["interrupted"]) == []
    assert "**Итог:** ГОТОВО" in report_of(repo, rid)
    with pytest.raises(LunaCoreError):
        await runner.resume(rid + "x")
    log.close()
    store.close()


async def test_a_run_outside_git_fails_with_a_report(tmp_path):
    (tmp_path / "repo").mkdir()
    runner, store, log = make_runner(tmp_path, TeamProvider({}))
    rid = ready_run(runner, store, plan_dict(ONE))
    with pytest.raises(TeamGitError):
        await runner.run(rid)
    run = store.run(rid)
    assert (run.status, run.outcome) == ("finished", "error")
    report = report_of(tmp_path / "repo", rid)
    assert "**Итог:** ОШИБКА" in report and "git repository" in report
    log.close()
    store.close()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_team_lifecycle.py -q`
Expected:
- FAIL: the restart test (no `interrupted`, no «прервано» note), the resume test (`AttributeError: 'TeamRunner' object has no attribute 'resume'`) and the outside-git test (the run stays `running`);
- PASS already: the planning test.

- [ ] **Step 3: Add recovery and resume to `runner.py`**

Replace the start of `run()`, from the `git = TeamGit(...)` line through `st = self._new_state(...)`, with:

```python
        git = TeamGit(self.root, run_id)
        session = Session.open(self.log, run.session_id, undo=False)
        resumed = run_id in self._seed
        if run.branch is None:
            try:
                base = await asyncio.to_thread(git.start)
            except LunaCoreError as exc:
                self._fail_early(run_id, exc)
                raise
            self.store.update_run(run_id, branch=git.branch, base=base, started=self.clock())
        else:
            await asyncio.to_thread(git.ensure)
            await self._recover(run_id, git, announce=not resumed)
        st = self._new_state(self._run(run_id), git, session)
```

Add these methods to `TeamRunner`. Put `resume` and `interrupted` after `cancel`, and the rest at the start of `# --- the end ---`:

```python
    async def resume(self, run_id: str) -> str:
        """Continue a finished run: failed and stuck tasks go back to the board."""
        run = self._run(run_id)
        if run.status != "finished" or run.branch is None:
            raise LunaCoreError(f"team {run_id} cannot be resumed (it is {run.status})")
        tasks = self.store.tasks(run_id)
        open_fixes = {t.merge_of for t in tasks if t.merge_of and t.status not in ("done", "failed")}
        for task in tasks:
            if task.status == "failed" or (task.status == "blocked" and task.id not in open_fixes):
                self.store.update_task(run_id, task.id, status="pending", attempts=0)
        report = self.root / ".luna" / "teams" / run_id / "report.md"
        previous = report.read_text(encoding="utf-8") if report.is_file() else ""
        self.store.update_run(
            run_id, status="running", outcome=None, stop_reason=None, finished=None,
            started=self.clock(),
        )  # fmt: skip
        self.store.log_event(run_id, "resumed")
        self._seed[run_id] = [f"Resumed by the user. The previous report:\n{previous[:6000]}"]
        return await self.run(run_id)

    def interrupted(self) -> list[str]:
        """Runs of this project left ``running`` (a crash or restart) and not running here."""
        return [
            r.id
            for r in self.store.runs(workdir=str(self.root), status="running")
            if r.id not in self._states and r.branch is not None
        ]

    # --- the end ----------------------------------------------------------

    def _fail_early(self, run_id: str, exc: Exception) -> None:
        """The run could not even start: end it ``error`` with a report."""
        self.store.log_event(run_id, "error", message=str(exc))
        self.store.update_run(run_id, status="finished", outcome="error", finished=self.clock())
        report = build_report(self.store, run_id)
        write_report(self.root, run_id, report)
        self.store.log_event(run_id, "team_done", outcome="error", headline=report.data["headline"])

    async def _salvage(self, run_id: str, git: TeamGit, note: str) -> list[str]:
        """Commit and drop worktrees of tasks still in progress; put those tasks back."""
        moved = []
        for task in self.store.tasks(run_id):
            if task.status != "in_progress":
                continue
            branch = task.branch
            if task.worktree and branch:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(git.salvage, Path(task.worktree), branch)
            keep = bool(branch) and await asyncio.to_thread(git.has_branch, branch)
            self.store.update_task(
                run_id, task.id, status="pending", worktree=None,
                branch=branch if keep else None, note=note,
            )  # fmt: skip
            moved.append(task.id)
        return moved

    async def _recover(self, run_id: str, git: TeamGit, *, announce: bool) -> None:
        """After a restart: in-progress tasks back to pending, members idle, note it."""
        at = time.strftime("%H:%M", time.localtime(self.clock()))
        moved = await self._salvage(run_id, git, f"прервано в {at}, продолжено")
        for member in self.store.members(run_id):
            if member.status == "working":
                self.store.update_member(run_id, member.name, status="idle")
        if announce:
            self.store.log_event(run_id, "interrupted", at=at, tasks=moved)
```

The section header `# --- the end ---` now sits above `_fail_early`. Remove the old duplicate header that stood above `_abort`.

In `_finish`, before `run = self._run(st.run_id)`, add:

```python
        if outcome == "error":
            await self._salvage(st.run_id, st.git, "the run failed")
```

- [ ] **Step 4: Run every team test**

Run: `uv run pytest packages/luna-core/tests -k "team or rategate or roles" -q`
Expected: PASS.

If the restart test hangs in the first phase, the `hang` coroutine is not reached. Check that `TeamProvider.before` is awaited before the delay.

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/team/runner.py packages/luna-core/tests/test_team_lifecycle.py
git commit -m "feat(team): restart recovery, resume and a reported early failure"
```

---

### Task 13: Public API and docs

**Files:**
- Modify: `packages/luna-core/src/luna_core/team/__init__.py`
- Modify: `packages/luna-core/src/luna_core/__init__.py`
- Modify: `packages/luna-core/tests/test_public_api.py`
- Modify: `AGENTS.md` (the `packages/luna-core/src/luna_core/` list in «Структура»)

**Interfaces:**
- Produces:
  - `luna_core.team` exports `TeamRunner`, `TeamHost`, `TeamSettings`, `TeamStore`, `TeamPlan`, `PlanError`, `Mailbox`, `build_report`, `keep_awake`;
  - `luna_core` exports `RateGate`, `TeamRunner`, `TeamHost`, `TeamSettings`, `TeamStore`.

- [ ] **Step 1: Write the failing test**

In `packages/luna-core/tests/test_public_api.py`, add to `EXPECTED` the names `"RateGate", "TeamRunner", "TeamHost", "TeamSettings", "TeamStore"`, and append:

```python
def test_team_package_exports():
    from luna_core import team

    assert set(team.__all__) == {
        "Mailbox", "PlanError", "TeamHost", "TeamPlan", "TeamRunner", "TeamSettings",
        "TeamStore", "build_report", "keep_awake",
    }  # fmt: skip
    assert luna_core.TeamRunner is team.TeamRunner
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest packages/luna-core/tests/test_public_api.py -q`
Expected: FAIL — the names are missing from `__all__`.

- [ ] **Step 3: Implement**

`packages/luna-core/src/luna_core/team/__init__.py`:

```python
"""Team mode: a lead and members working a shared board (spec part 2)."""

from luna_core.team.board import TeamStore
from luna_core.team.inhibit import keep_awake
from luna_core.team.messages import Mailbox
from luna_core.team.plan import PlanError, TeamPlan
from luna_core.team.report import build_report
from luna_core.team.runner import TeamHost, TeamRunner
from luna_core.team.settings import TeamSettings

__all__ = [
    "Mailbox", "PlanError", "TeamHost", "TeamPlan", "TeamRunner", "TeamSettings", "TeamStore",
    "build_report", "keep_awake",
]  # fmt: skip
```

In `packages/luna-core/src/luna_core/__init__.py`, add the imports after the existing ones (keep isort order):

```python
from luna_core.rategate import RateGate
from luna_core.team import TeamHost, TeamRunner, TeamSettings, TeamStore
```

and add `"RateGate", "TeamHost", "TeamRunner", "TeamSettings", "TeamStore"` to `__all__`.

If importing `luna_core.team` from `luna_core/__init__.py` causes a circular import, the cause is that `team.runner` imports `luna_core.loop` while `luna_core` itself is still initialising. Move the two new import lines to the end of the import block. Every other module is already loaded by then.

In `AGENTS.md`, under `- packages/luna-core/src/luna_core/ — переносимость между ОС`, add after the `fsperm.py` line:

```markdown
  - `rategate.py` — общий для процесса rate gate: токен-бакет, пауза по
    `Retry-After` и снижение темпа при 429, медленное восстановление
  - `team/` — режим команды: `board.py` (доска в `sessions.db`), `messages.py`,
    `plan.py` (план и read-only лидер на роли `strong`), `permissions.py`
    (профиль прав без вопросов), `git.py` (ветка `luna/team/<id>`, worktree
    участников, последовательное вливание), `tools.py`, `report.py`,
    `inhibit.py`, `runner.py` (`TeamRunner`)
```

- [ ] **Step 4: Run the whole suite and the linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: the whole suite passes, with the same skips as before plus none new, and ruff is clean.

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/__init__.py packages/luna-core/src/luna_core/team/__init__.py packages/luna-core/tests/test_public_api.py AGENTS.md
git commit -m "feat(team): export the team engine and the rate gate; document the package"
```
