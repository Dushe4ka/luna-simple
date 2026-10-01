import pytest

from luna.core.session_state import SessionStateStore
from luna.server.runtime import RuntimeRegistry, WorkdirMismatch


def _registry(built, capacity=8):
    def build(runtime):
        built.append((runtime.thread_id, runtime.config().provider, runtime.config().model))
        return object()

    return RuntimeRegistry(build, capacity=capacity)


def test_each_session_gets_its_own_agent(tmp_path):
    built = []
    reg = _registry(built)
    a, b = reg.get("t1", str(tmp_path)), reg.get("t2", str(tmp_path))
    assert a.agent is not b.agent
    assert reg.get("t1", str(tmp_path)) is a


def test_switch_model_rebuilds_and_persists(tmp_path):
    built = []
    rt = _registry(built).get("t1", str(tmp_path))
    rt.agent  # noqa: B018 - first build
    rt.switch_model("gpt-5")
    assert built[-1][2] == "gpt-5"
    assert SessionStateStore().load("t1", str(tmp_path)).model == "gpt-5"


def test_failed_switch_restores_the_previous_model(tmp_path):
    calls = {"n": 0}

    def build(runtime):
        calls["n"] += 1
        if runtime.state.model == "bad":
            raise RuntimeError("unknown model")
        return object()

    rt = RuntimeRegistry(build).get("t1", str(tmp_path))
    rt.agent  # noqa: B018
    with pytest.raises(RuntimeError):
        rt.switch_model("bad")
    assert rt.state.model is None
    assert SessionStateStore().load("t1", str(tmp_path)).model is None


def test_switch_provider_resets_the_model(tmp_path):
    rt = _registry([]).get("t1", str(tmp_path))
    rt.switch_model("m1")
    rt.switch_provider("openai")
    assert (rt.state.provider, rt.state.model) == ("openai", None)
    assert rt.config().provider == "openai"


def test_plan_flag_is_per_session(tmp_path):
    reg = _registry([])
    one, two = reg.get("t1", str(tmp_path)), reg.get("t2", str(tmp_path))
    one.set_plan(True)
    assert one.state.plan is True and two.state.plan is False


def test_lru_evicts_idle_runtimes_and_reloads_state(tmp_path):
    reg = _registry([], capacity=1)
    first = reg.get("t1", str(tmp_path))
    first.set_plan(True)
    reg.get("t2", str(tmp_path))
    again = reg.get("t1", str(tmp_path))
    assert again is not first and again.state.plan is True


def test_same_thread_other_workdir_is_refused(tmp_path):
    reg = _registry([])
    reg.get("t1", str(tmp_path))
    with pytest.raises(WorkdirMismatch):
        reg.get("t1", str(tmp_path / "other"))
