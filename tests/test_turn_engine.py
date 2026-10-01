from types import SimpleNamespace

from luna.core.session_state import SessionState
from luna.turn.engine import PreparedTurn, delegate_line, prepare_turn


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
