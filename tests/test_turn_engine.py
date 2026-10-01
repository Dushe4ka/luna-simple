from types import SimpleNamespace

from luna.config.config import LunaConfig
from luna.config.usage import TurnUsage
from luna.core.session_state import SessionState
from luna.turn import engine
from luna.turn.engine import (
    FinishResult,
    Notice,
    PreparedTurn,
    TurnOutcome,
    delegate_line,
    finish_fixup,
    finish_turn,
    prepare_turn,
)


class _Agent:
    def get_state(self, config):
        return SimpleNamespace(values={"messages": []})


def _prepare(state, line, tmp_path, subagents=frozenset()):
    return prepare_turn(
        state,
        line,
        agent=_Agent(),
        thread_id=state.thread_id,
        workdir=str(tmp_path),
        session_id=state.thread_id,
        subagent_names=set(subagents),
    )


def test_plain_line_passes_through(tmp_path):
    prepared = _prepare(SessionState("t1", str(tmp_path)), "hello", tmp_path)
    assert isinstance(prepared, PreparedTurn)
    assert prepared.content == "hello" and prepared.title_line == "hello"


def test_pinned_files_are_prepended_fresh_from_disk(tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n")
    state = SessionState("t1", str(tmp_path))
    state.pinned.add("a.py")
    prepared = _prepare(state, "look", tmp_path)
    assert "print(1)" in prepared.content
    assert prepared.content.rstrip().endswith("look")


def test_pending_diagnostics_are_sent_once_then_cleared(tmp_path):
    state = SessionState("t1", str(tmp_path), pending_diagnostics="a.py:1 E501")
    first = _prepare(state, "go", tmp_path)
    assert first.content.startswith("<diagnostics>\na.py:1 E501\n</diagnostics>")
    assert state.pending_diagnostics == ""
    assert "<diagnostics>" not in _prepare(state, "again", tmp_path).content


def test_at_agent_delegates_only_for_known_subagents(tmp_path):
    assert delegate_line("@researcher find X", {"researcher"}).startswith(
        "Delegate this to the 'researcher' subagent"
    )
    assert delegate_line("@nobody find X", {"researcher"}) == "@nobody find X"
    state = SessionState("t1", str(tmp_path))
    prepared = _prepare(state, "@researcher find X", tmp_path, {"researcher"})
    assert prepared.title_line.startswith("Delegate this to the 'researcher'")


def test_at_file_mentions_are_expanded(tmp_path):
    (tmp_path / "notes.md").write_text("secret sauce\n")
    prepared = _prepare(SessionState("t1", str(tmp_path)), "read @notes.md", tmp_path)
    assert "secret sauce" in prepared.content


class _Index:
    def __init__(self):
        self.recorded, self.touched = [], []

    def record(self, thread_id, workdir, title):
        self.recorded.append((thread_id, workdir, title))

    def touch(self, thread_id):
        self.touched.append(thread_id)


def _finish(state, outcome, cfg, tmp_path, index=None):
    prepared = PreparedTurn(content="x", title_line="fix the bug", dirty_before=[])
    return finish_turn(
        state, prepared, outcome, cfg=cfg, index=index, thread_id="t1", workdir=str(tmp_path)
    )


def test_read_only_turn_records_usage_and_index_without_verify(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "run_verify", lambda *a: (_ for _ in ()).throw(AssertionError))
    state, index = SessionState("t1", str(tmp_path)), _Index()
    cfg = LunaConfig(workdir=str(tmp_path), verify_command="pytest")
    result = _finish(state, TurnOutcome(usage=TurnUsage(10, 2, 12)), cfg, tmp_path, index)
    assert result == FinishResult(notices=[], fixup_prompt=None, reload=False)
    assert state.usage.totals == (10, 2, 12)
    assert index.recorded == [("t1", str(tmp_path), "fix the bug")] and index.touched == ["t1"]


def test_mutating_turn_with_failing_verify_asks_for_one_fixup(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "run_verify", lambda cmd, wd: (False, "1 failed"))
    monkeypatch.setattr(engine, "format_and_diagnose", lambda cfg, before: ([], "a.py:1 E1"))
    state = SessionState("t1", str(tmp_path))
    cfg = LunaConfig(workdir=str(tmp_path), verify_command="pytest")
    result = _finish(state, TurnOutcome(tool_names={"edit_file"}), cfg, tmp_path)
    assert result.fixup_prompt == "The verify command `pytest` failed. Output:\n1 failed\nFix it."
    assert Notice("warn", "verify failed\n1 failed") in result.notices
    assert state.pending_diagnostics == "a.py:1 E1"


def test_passing_verify_reports_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "run_verify", lambda cmd, wd: (True, ""))
    monkeypatch.setattr(engine, "format_and_diagnose", lambda cfg, before: ([], ""))
    cfg = LunaConfig(workdir=str(tmp_path), verify_command="pytest")
    state = SessionState("t1", str(tmp_path))
    result = _finish(state, TurnOutcome(tool_names={"execute"}), cfg, tmp_path)
    assert result.notices == [Notice("ok", "✓ verify ok")] and result.fixup_prompt is None


def test_reload_request_is_passed_through(tmp_path):
    result = _finish(
        SessionState("t1", str(tmp_path)),
        TurnOutcome(reload_requested=True),
        LunaConfig(workdir=str(tmp_path)),
        tmp_path,
    )
    assert result.reload is True


def test_finish_fixup_reports_the_retry_result(tmp_path, monkeypatch):
    cfg = LunaConfig(workdir=str(tmp_path), verify_command="pytest")
    monkeypatch.setattr(engine, "run_verify", lambda cmd, wd: (False, "still red"))
    assert finish_fixup(cfg) == [Notice("warn", "⚠ verify still failing after 1 retry\nstill red")]
    monkeypatch.setattr(engine, "run_verify", lambda cmd, wd: (True, ""))
    assert finish_fixup(cfg) == [Notice("ok", "✓ verify ok")]
