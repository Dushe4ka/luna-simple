import io

from langchain_core.messages import AIMessage
from rich.console import Console

from luna.agent import build_agent
from luna.config import LunaConfig
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
    assert (tmp_path / ".luna" / "undo" / "sid" / "0000.json").exists()
    assert "notes.txt" in out  # /diff showed the change
    assert "undo cancelled" in out  # scripted "n" declined it
    assert (tmp_path / "notes.txt").exists()  # not reverted
