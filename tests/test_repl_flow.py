import io

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


def test_user_command_fallthrough_double_expands_at_mentions(tmp_path, fake_model):
    """Empirical check of the double-``expand_mentions`` question from the brief.

    A user command's body containing an ``@file`` mention is expanded once by
    ``usercmd.expand`` (inside ``dispatch``) and the resulting text is left in
    ``line``, which then falls through to the normal turn-building code in
    ``run_repl`` — which calls ``expand_mentions`` on it *again*. The brief
    speculated this second pass is harmless because the text "no longer
    contains unexpanded @file tokens after the first pass". That premise is
    false: ``expand_mentions`` only *appends* an ``<attached>`` block, it does
    not remove or rewrite the original ``@file`` token from the text — so the
    literal ``@notes.txt`` substring survives the first pass and is matched
    again by the second, attaching the file's content a second time. This
    test pins that observed behaviour (content duplicated, not corrupted or
    crashing) so a future change to either function is forced to notice it.
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
    # The @notes.txt mention survives usercmd.expand's own pass (expand_mentions
    # only appends, it never strips the original token), so run_repl's second
    # expand_mentions(line, workdir) call re-matches it and attaches the file
    # a second time. Empirically NOT a no-op: content is duplicated.
    assert payload.count("NOTE_CONTENT") == 2
    assert payload.count("<attached: notes.txt>") == 2
    assert "hi @notes.txt world" in payload

    # The title, recorded from the reassigned `line` (the expanded prompt, not
    # the raw "/greet world"), must reflect that expansion correctly and must
    # not be corrupted by the double-expansion or by unrelated REPL output
    # (see test_repl_records_prompt_not_indicator_line_as_session_title).
    row = idx.latest_for(str(tmp_path))
    assert row is not None
    user_commands = load_user_commands(str(tmp_path))
    from luna.usercmd import expand as usercmd_expand

    expanded_once = usercmd_expand(user_commands["greet"], "world", str(tmp_path))
    assert row.title == make_title(expanded_once)
    assert "ctx " not in row.title
    assert "$" not in row.title


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
