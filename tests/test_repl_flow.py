import io
import subprocess
import sys

from langchain_core.messages import AIMessage
from rich.console import Console

from luna.agent import build_agent
from luna.config import LunaConfig
from luna.persistence import SessionIndex, make_title
from luna.session import run_repl
from luna.usercmd import load as load_user_commands


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


def test_user_command_fallthrough_expands_at_mentions_exactly_once(tmp_path, fake_model):
    """Regression test for the M2 fix (final whole-branch review): usercmd.expand no
    longer runs expand_mentions itself, so a custom command's @file mention is expanded
    exactly once by run_repl's own turn-building call — not twice.

    Before the fix, a user command's body containing an ``@file`` mention was expanded
    once by ``usercmd.expand`` (inside ``dispatch``) and the resulting text — which
    still contained the literal, unremoved ``@file`` token — fell through to
    ``run_repl``'s own ``expand_mentions`` call, attaching the file's content a second
    time. usercmd.expand now leaves ``@file``/``@agent`` tokens untouched, so only
    run_repl's single pass expands them.
    """
    commands_dir = tmp_path / ".luna" / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "greet.md").write_text("hi @notes.txt $ARGUMENTS\n")
    (tmp_path / "notes.txt").write_text("NOTE_CONTENT\n")

    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True),
        model=fake_model(AIMessage(content="ok")),
        session_id="sid3",
    )
    seen_payloads: list[str] = []
    real_stream = agent.stream

    def _spy(payload, *a, **kw):
        if isinstance(payload, dict):
            msgs = payload.get("messages", [])
            if msgs:
                seen_payloads.append(msgs[-1].get("content", ""))
        return real_stream(payload, *a, **kw)

    agent.stream = _spy
    console = Console(file=io.StringIO(), force_terminal=True)
    lines = iter(["/greet world", "/exit"])
    idx = SessionIndex()

    rc = run_repl(
        agent,
        console=console,
        input_fn=lambda _: next(lines),
        rebuild=lambda: agent,
        index=idx,
        workdir=str(tmp_path),
        config=LunaConfig(workdir=str(tmp_path), yolo=True),
        thread_id="t3",
        session_id="sid3",
    )
    assert rc == 0
    assert len(seen_payloads) == 1
    payload = seen_payloads[0]
    assert payload.count("NOTE_CONTENT") == 1
    assert payload.count("<attached: notes.txt>") == 1
    assert "hi @notes.txt world" in payload

    # The title, recorded from the reassigned `line` (the expanded prompt, not
    # the raw "/greet world"), must reflect that single expansion correctly and
    # must not be corrupted by unrelated REPL output (see
    # test_repl_records_prompt_not_indicator_line_as_session_title).
    row = idx.latest_for(str(tmp_path))
    assert row is not None
    user_commands = load_user_commands(str(tmp_path))
    from luna.usercmd import expand as usercmd_expand

    expanded_once = usercmd_expand(user_commands["greet"], "world", str(tmp_path))
    assert expanded_once == "hi @notes.txt world"
    assert row.title == make_title(expanded_once)
    assert "ctx " not in row.title
    assert "$" not in row.title


def test_user_command_body_starting_with_at_agent_delegates_cleanly(tmp_path, fake_model):
    """A .luna/commands/*.md body that itself starts with @<known-subagent> must
    delegate cleanly via run_repl's @agent rewrite, not get a spurious '(...: not
    found)' file-mention annotation injected before the @agent check runs (M2)."""
    commands_dir = tmp_path / ".luna" / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "ask.md").write_text("@researcher $ARGUMENTS")

    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(AIMessage(content="ok"))
    )
    seen_payloads: list[str] = []
    real_stream = agent.stream

    def _spy(payload, *a, **kw):
        if isinstance(payload, dict):
            msgs = payload.get("messages", [])
            if msgs:
                seen_payloads.append(msgs[-1].get("content", ""))
        return real_stream(payload, *a, **kw)

    agent.stream = _spy
    console = Console(file=io.StringIO(), force_terminal=True)
    lines = iter(["/ask find the docs", "/exit"])
    run_repl(
        agent,
        console=console,
        input_fn=lambda _: next(lines),
        rebuild=lambda: agent,
        workdir=str(tmp_path),
        config=LunaConfig(workdir=str(tmp_path), yolo=True),
        thread_id="t",
    )
    payload = seen_payloads[0]
    assert "not found" not in payload
    assert "researcher" in payload and "task tool" in payload


def test_diagnostics_are_injected_into_the_next_turn(tmp_path, fake_model):
    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": "1",
                    "args": {"file_path": "/a.py", "content": "x = 1\n"},
                }
            ],
        ),
        AIMessage(content="wrote a.py"),
        AIMessage(content="saw the diagnostics"),
    ]
    agent = build_agent(
        LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls), session_id="sid"
    )
    seen_payloads: list[str] = []
    real_stream = agent.stream

    def _spy(payload, *a, **kw):
        if isinstance(payload, dict):
            msgs = payload.get("messages", [])
            if msgs:
                seen_payloads.append(msgs[-1].get("content", ""))
        return real_stream(payload, *a, **kw)

    agent.stream = _spy
    console = Console(file=io.StringIO(), force_terminal=True)
    lines = iter(["write a.py", "second turn", "/exit"])
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True, diagnose_command="echo a.py:1: fake finding")
    run_repl(
        agent,
        console=console,
        input_fn=lambda _: next(lines),
        rebuild=lambda: agent,
        workdir=str(tmp_path),
        config=cfg,
        thread_id="t",
        session_id="sid",
    )
    assert any("<diagnostics>" in p and "fake finding" in p for p in seen_payloads)


def test_format_only_touches_files_the_turn_actually_changed(tmp_path, fake_model, monkeypatch):
    """A file the user had already modified (uncommitted) BEFORE the turn ran must
    not be passed to the formatter — only files that became newly dirty during the
    turn itself (the M5 fix)."""

    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    _git("init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git("add", "-A")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    # the user already had an uncommitted change to a.txt BEFORE the turn started
    (tmp_path / "a.txt").write_text("pre-existing uncommitted change\n")

    calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "id": "1",
                    "args": {"file_path": "/b.txt", "content": "new\n"},
                }
            ],
        ),
        AIMessage(content="wrote b.txt"),
    ]
    agent = build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model(*calls))

    import luna.fmt as fmt_module

    seen_paths: list[list[str]] = []
    real_run = fmt_module.run

    def _spy_run(command, workdir, paths):
        seen_paths.append(list(paths))
        return real_run(command, workdir, paths)

    monkeypatch.setattr(fmt_module, "run", _spy_run)

    console = Console(file=io.StringIO(), force_terminal=True)
    cfg = LunaConfig(workdir=str(tmp_path), yolo=True, format_command=f'{sys.executable} -c "pass"')
    lines = iter(["write b.txt", "/exit"])
    run_repl(
        agent,
        console=console,
        input_fn=lambda _: next(lines),
        rebuild=lambda: agent,
        workdir=str(tmp_path),
        config=cfg,
        thread_id="t",
    )
    assert seen_paths  # the formatter ran at least once
    touched = seen_paths[0]
    assert "b.txt" in touched
    assert "a.txt" not in touched  # pre-existing dirty file was left alone
