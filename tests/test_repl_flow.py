import io

from langchain_core.messages import AIMessage
from rich.console import Console

from luna.agent import build_agent
from luna.config import LunaConfig
from luna.persistence import SessionIndex, make_title
from luna.session import run_repl


def test_repl_flow_at_expansion_diff_undo(tmp_path, fake_model):
    (tmp_path / "README.md").write_text("# demo\n")
    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": "1",
                    "args": {"file_path": "/notes.txt", "content": "hi\n"},
                }
            ],
        ),
        AIMessage(content="wrote notes.txt"),
    ]
    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True),
        model=fake_model(*calls),
        session_id="sid",
    )
    console = Console(file=io.StringIO(), force_terminal=True)
    lines = iter(["@README.md summarise this", "/diff", "/undo", "n", "/exit"])

    rc = run_repl(
        agent,
        console=console,
        input_fn=lambda _: next(lines),
        rebuild=lambda: agent,
        workdir=str(tmp_path),
        config=LunaConfig(workdir=str(tmp_path), yolo=True),
        thread_id="t",
        session_id="sid",
    )
    assert rc == 0
    out = console.file.getvalue()
    assert list((tmp_path / ".luna" / "undo" / "sid").glob("[0-9]*.json"))
    assert "notes.txt" in out  # /diff showed the change
    assert "undo cancelled" in out  # scripted "n" declined it
    assert (tmp_path / "notes.txt").exists()  # not reverted


def test_repl_records_prompt_not_indicator_line_as_session_title(tmp_path, fake_model):
    """A real turn (with token usage) must not corrupt the persisted title.

    Regression for a bug where the dim usage indicator printed after a turn
    reused the ``line`` variable also holding the user's raw prompt, so the
    session title recorded via ``index.record`` ended up being the indicator
    string (``"ctx ~... · $0.0042"``) instead of the actual prompt text.
    """
    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True),
        model=fake_model(
            AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        ),
        session_id="sid2",
    )
    console = Console(file=io.StringIO(), force_terminal=True)
    prompt = "remember X please"
    lines = iter([prompt, "/exit"])
    idx = SessionIndex()

    rc = run_repl(
        agent,
        console=console,
        input_fn=lambda _: next(lines),
        rebuild=lambda: agent,
        index=idx,
        workdir=str(tmp_path),
        config=LunaConfig(workdir=str(tmp_path), yolo=True),
        thread_id="t2",
        session_id="sid2",
    )
    assert rc == 0
    row = idx.latest_for(str(tmp_path))
    assert row is not None
    assert row.title == make_title(prompt)
    assert "ctx " not in row.title
    assert "$" not in row.title
