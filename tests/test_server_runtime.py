import pytest
from luna_core.background import AgentManager
from luna_core.session import EventLog

from luna.core.session_state import SessionStateStore
from luna.server.runtime import RuntimeRegistry, WorkdirMismatch


class _Bundle:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _registry(tmp_path, built, capacity=8, build=None):
    async def default_build(runtime):
        cfg = runtime.config()
        built.append((runtime.id, cfg.provider, cfg.model))
        return _Bundle()

    return RuntimeRegistry(
        build or default_build,
        log=EventLog(tmp_path / "log.db"),
        manager=AgentManager(),
        capacity=capacity,
    )


async def test_each_session_gets_its_own_bundle(tmp_path):
    reg = _registry(tmp_path, [])
    a, b = await reg.get("t1", str(tmp_path)), await reg.get("t2", str(tmp_path))
    assert await a.bundle() is not await b.bundle()
    assert await reg.get("t1", str(tmp_path)) is a
    assert await a.bundle() is await a.bundle()  # built once


async def test_an_unknown_id_creates_the_session_with_that_id(tmp_path):
    reg = _registry(tmp_path, [])
    await reg.get("fresh", str(tmp_path))
    assert reg.log.info("fresh").workdir == str(tmp_path.resolve())


async def test_switch_model_rebuilds_closes_the_old_bundle_and_persists(tmp_path):
    built = []
    rt = await _registry(tmp_path, built).get("t1", str(tmp_path))
    old = await rt.bundle()
    await rt.switch_model("gpt-5")
    assert built[-1][2] == "gpt-5" and old.closed
    assert SessionStateStore().load("t1", str(tmp_path)).model == "gpt-5"


async def test_failed_switch_restores_the_previous_model_and_bundle(tmp_path):
    async def build(runtime):
        if runtime.state.model == "bad":
            raise RuntimeError("unknown model")
        return _Bundle()

    rt = await _registry(tmp_path, [], build=build).get("t1", str(tmp_path))
    old = await rt.bundle()
    with pytest.raises(RuntimeError):
        await rt.switch_model("bad")
    assert rt.state.model is None and await rt.bundle() is old and not old.closed
    assert SessionStateStore().load("t1", str(tmp_path)).model is None


async def test_switch_provider_resets_the_model(tmp_path):
    rt = await _registry(tmp_path, []).get("t1", str(tmp_path))
    await rt.switch_model("m1")
    await rt.switch_provider("openai")
    assert (rt.state.provider, rt.state.model) == ("openai", None)
    assert rt.config().provider == "openai"


async def test_plan_flag_is_per_session(tmp_path):
    reg = _registry(tmp_path, [])
    one, two = await reg.get("t1", str(tmp_path)), await reg.get("t2", str(tmp_path))
    one.set_plan(True)
    assert one.state.plan is True and two.state.plan is False


async def test_lru_evicts_idle_runtimes_closes_them_and_reloads_state(tmp_path):
    reg = _registry(tmp_path, [], capacity=1)
    first = await reg.get("t1", str(tmp_path))
    bundle = await first.bundle()
    first.set_plan(True)
    await reg.get("t2", str(tmp_path))
    assert bundle.closed
    again = await reg.get("t1", str(tmp_path))
    assert again is not first and again.state.plan is True


async def test_same_session_other_workdir_is_refused(tmp_path):
    reg = _registry(tmp_path, [])
    await reg.get("t1", str(tmp_path))
    with pytest.raises(WorkdirMismatch):
        await reg.get("t1", str(tmp_path / "other"))
    reg._items.clear()  # also when it is not loaded
    with pytest.raises(WorkdirMismatch):
        await reg.get("t1", str(tmp_path / "other"))


async def test_a_runtime_waiting_for_approval_is_never_evicted(tmp_path):
    reg = _registry(tmp_path, [], capacity=1)
    waiting = await reg.get("t1", str(tmp_path))
    waiting.session.expect_decision("c1")  # paused on an approval, lock released
    await reg.get("t2", str(tmp_path))
    assert await reg.get("t1", str(tmp_path)) is waiting


async def test_a_running_session_is_never_evicted(tmp_path):
    reg = _registry(tmp_path, [], capacity=1)
    running = await reg.get("t1", str(tmp_path))
    running.session._begin_turn()
    try:
        await reg.get("t2", str(tmp_path))
        assert await reg.get("t1", str(tmp_path)) is running
    finally:
        running.session._end_turn()


async def test_a_session_with_a_mutating_command_in_flight_is_never_evicted(tmp_path):
    reg = _registry(tmp_path, [], capacity=1)
    busy = await reg.get("t1", str(tmp_path))
    async with busy.lock:
        await reg.get("t2", str(tmp_path))
        assert reg.peek("t1") is busy


async def test_a_failed_auto_reload_is_an_out_of_turn_notice(tmp_path):
    calls = []

    async def build(runtime):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("bad mcp.json")
        return _Bundle()

    rt = await _registry(tmp_path, [], build=build).get("t1", str(tmp_path))
    await rt.bundle()
    seen = []
    rt.listen(seen.append)
    rt.reload_after_turn = True
    await rt.after_turn()
    assert seen == [
        {"event": "notice", "level": "error", "text": "auto-reload failed: bad mcp.json"}
    ]
    assert rt.reload_after_turn is False
