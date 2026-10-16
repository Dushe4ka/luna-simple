import asyncio
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from luna_core.events import TextDelta
from luna_core.messages import Message, Text, ToolCall
from luna_core.providers.fake import FakeProvider
from luna_core.session import Session
from rich.console import Console

from luna.config.config import LunaConfig
from luna.core.agent import build_agent
from luna.core.session import run_once, run_repl
from luna.core.store import make_title, open_log
from luna.repl.usercmd import expand as usercmd_expand
from luna.repl.usercmd import load as load_user_commands
from luna.turn import engine


def console(**kw):
    kw.setdefault("force_terminal", True)
    return Console(file=io.StringIO(), **kw)


def _cfg(tmp_path, **kw):
    kw.setdefault("format_command", "")
    kw.setdefault("diagnose_command", "")
    return LunaConfig(workdir=str(tmp_path), model="m", **kw)


async def _repl(tmp_path, script, lines, *, cfg=None, out=None, hooks=False, rebuild=None):
    """Run the REPL over ``script`` with scripted input; returns (code, out, session, provider)."""
    cfg = cfg or _cfg(tmp_path, yolo=True)
    provider = FakeProvider(script)
    hooks_state: dict = {}
    pipeline = engine.make_hooks(lambda: hooks_state.get("state"), lambda: cfg) if hooks else None
    bundle = await build_agent(cfg, provider=provider, hooks=pipeline)
    out = out or console()
    log = open_log()
    session = Session.create(log, str(Path(tmp_path).resolve()))
    answers = iter(lines)
    try:
        code = await run_repl(
            bundle,
            console=out,
            input_fn=lambda _prompt: next(answers),
            rebuild=rebuild,
            log=log,
            session=session,
            config=cfg,
            plan_state=[False],
            hooks_state=hooks_state,
        )
    finally:
        await bundle.aclose()
    return code, out.file.getvalue(), session, provider


def _user_texts(session):
    return [m.text for m in session.messages() if m.role == "user"]


async def test_run_once_text_and_json(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    bundle = await build_agent(
        cfg, provider=FakeProvider([FakeProvider.text("hello"), FakeProvider.text("again")])
    )
    log = open_log()
    assert await run_once(bundle, "hi", console=console(), log=log, cfg=cfg) == "hello"
    await run_once(bundle, "hi", console=console(), log=log, cfg=cfg, output_format="json")
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["text"] == "again" and payload["session_id"] and payload["usage"]["input"] == 10
    assert payload["error"] is None
    await bundle.aclose()


async def test_run_once_titles_the_session_with_the_prompt(tmp_path):
    cfg = _cfg(tmp_path)
    bundle = await build_agent(cfg, provider=FakeProvider([FakeProvider.text("ok")]))
    log = open_log()
    await run_once(bundle, "  fix   the\nbug ", console=console(), log=log, cfg=cfg)
    (info,) = log.list(str(Path(tmp_path).resolve()))
    assert info.title == "fix the bug"
    await bundle.aclose()


async def test_repl_approval_reject_with_reason(tmp_path):
    script = [
        FakeProvider.call("write_file", {"file_path": "a.txt", "content": "x"}),
        FakeProvider.text("ok"),
    ]
    code, out, session, _ = await _repl(
        tmp_path, script, ["write it", "n", "not now", "/exit"], cfg=_cfg(tmp_path)
    )
    assert code == 0 and not (tmp_path / "a.txt").exists()
    assert "rejected by the user: not now" in session.messages()[2].results[0].text


async def test_repl_flow_at_expansion_diff_undo(tmp_path):
    (tmp_path / "README.md").write_text("# demo\n")
    script = [
        FakeProvider.call("write_file", {"file_path": "/notes.txt", "content": "hi\n"}),
        FakeProvider.text("wrote notes.txt"),
    ]
    code, out, session, _ = await _repl(
        tmp_path, script, ["@README.md summarise this", "/diff", "/undo", "n", "/exit"]
    )
    assert code == 0
    assert "<attached: README.md>" in _user_texts(session)[0]
    assert (tmp_path / ".luna" / "undo" / session.id / "journal.json").exists()
    assert "notes.txt" in out.split("wrote notes.txt")[-1]  # /diff showed the change
    assert "undo cancelled" in out  # scripted "n" declined it
    assert (tmp_path / "notes.txt").exists()  # not reverted


async def test_repl_undo_confirmed_reverts_files_and_conversation(tmp_path):
    script = [
        FakeProvider.call("write_file", {"file_path": "notes.txt", "content": "hi\n"}),
        FakeProvider.text("wrote it"),
    ]
    code, out, session, _ = await _repl(
        tmp_path, script, ["write notes", "/undo", "y", "/redo", "/exit"]
    )
    assert code == 0 and "files restored; conversation rewound" in out
    assert "files re-applied; turn restored" in out
    assert (tmp_path / "notes.txt").read_text() == "hi\n"  # redo re-applied it


async def test_repl_records_prompt_not_indicator_line_as_session_title(tmp_path):
    """A turn with usage must not put the usage line into the session title."""
    prompt = "remember X please"
    code, out, session, _ = await _repl(tmp_path, [FakeProvider.text("ok")], [prompt, "/exit"])
    assert code == 0 and "10 in · 5 out" in out
    assert session.title == make_title(prompt)
    assert " in · " not in session.title and "$" not in session.title


async def test_user_command_fallthrough_expands_at_mentions_exactly_once(tmp_path):
    """A custom command's @file is expanded once, by the REPL's own turn preparation."""
    commands_dir = tmp_path / ".luna" / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "greet.md").write_text("hi @notes.txt $ARGUMENTS\n")
    (tmp_path / "notes.txt").write_text("NOTE_CONTENT\n")
    code, _, session, provider = await _repl(
        tmp_path, [FakeProvider.text("ok")], ["/greet world", "/exit"]
    )
    assert code == 0 and len(provider.requests) == 1
    (payload,) = _user_texts(session)
    assert payload.count("NOTE_CONTENT") == 1
    assert payload.count("<attached: notes.txt>") == 1
    assert "hi @notes.txt world" in payload
    expanded_once = usercmd_expand(
        load_user_commands(str(tmp_path))["greet"], "world", str(tmp_path)
    )
    assert expanded_once == "hi @notes.txt world"
    assert session.title == make_title(expanded_once)


async def test_user_command_body_starting_with_at_agent_delegates_cleanly(tmp_path):
    commands_dir = tmp_path / ".luna" / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "ask.md").write_text("@researcher $ARGUMENTS")
    _, _, session, _ = await _repl(
        tmp_path, [FakeProvider.text("ok")], ["/ask find the docs", "/exit"]
    )
    (payload,) = _user_texts(session)
    assert "not found" not in payload
    assert "researcher" in payload and "task tool" in payload


async def test_diagnostics_are_injected_into_the_next_turn(tmp_path):
    script = [
        FakeProvider.call("write_file", {"file_path": "/a.py", "content": "x = 1\n"}),
        FakeProvider.text("wrote a.py"),
        FakeProvider.text("saw the diagnostics"),
    ]
    cfg = _cfg(tmp_path, yolo=True, diagnose_command="echo a.py:1: fake finding")
    _, out, session, _ = await _repl(
        tmp_path, script, ["write a.py", "second turn", "/exit"], cfg=cfg, hooks=True
    )
    first, second = _user_texts(session)
    assert "<diagnostics>" not in first
    assert second.startswith("<diagnostics>") and "fake finding" in second
    assert "fake finding" in out  # shown as a dim notice after the first turn


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


async def test_format_only_touches_files_the_turn_actually_changed(tmp_path, monkeypatch):
    """A file the user had already modified before the turn is not formatted."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("v0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "a.txt").write_text("pre-existing uncommitted change\n")

    import luna.turn.fmt as fmt_module

    seen_paths: list[list[str]] = []
    real_run = fmt_module.run
    monkeypatch.setattr(
        fmt_module, "run", lambda c, w, p: seen_paths.append(list(p)) or real_run(c, w, p)
    )
    script = [
        FakeProvider.call("write_file", {"file_path": "/b.txt", "content": "new\n"}),
        FakeProvider.text("wrote b.txt"),
    ]
    cfg = _cfg(tmp_path, yolo=True, format_command=f'{sys.executable} -c "pass"')
    await _repl(tmp_path, script, ["write b.txt", "/exit"], cfg=cfg, hooks=True)
    assert seen_paths == [["b.txt"]]  # neither a.txt nor Luna's own .luna/ state


async def test_format_is_never_run_whole_project_when_the_turn_touches_only_pre_dirty_files(
    tmp_path, monkeypatch
):
    """An empty scoped delta must not be treated as "format everything"."""
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".luna/\n")
    (tmp_path / "a.txt").write_text("v0\n")
    (tmp_path / "c.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "a.txt").write_text("pre-existing uncommitted change\n")

    import luna.turn.fmt as fmt_module

    fmt_calls: list[list[str]] = []
    real_run = fmt_module.run
    monkeypatch.setattr(
        fmt_module, "run", lambda c, w, p: fmt_calls.append(list(p)) or real_run(c, w, p)
    )
    script = [
        FakeProvider.call("write_file", {"file_path": "/a.txt", "content": "edited too\n"}),
        FakeProvider.text("edited a.txt"),
    ]
    cfg = _cfg(tmp_path, yolo=True, format_command=f'{sys.executable} -c "pass"')
    await _repl(tmp_path, script, ["edit a.txt again", "/exit"], cfg=cfg, hooks=True)
    assert fmt_calls == []


async def test_tool_progress_prints_an_informative_label_and_done_line(tmp_path):
    script = [
        FakeProvider.call("write_file", {"file_path": "/notes.txt", "content": "hi\n"}),
        FakeProvider.text("wrote notes.txt"),
    ]
    code, out, _, _ = await _repl(
        tmp_path, script, ["write notes.txt please", "/exit"], out=console(no_color=True)
    )
    assert code == 0
    assert "write_file(/notes.txt) · done" in out
    assert "wrote notes.txt" in out


async def test_tool_progress_handles_two_parallel_tool_calls(tmp_path):
    both = Message.assistant(
        [
            ToolCall("1", "write_file", {"file_path": "/a.txt", "content": "a\n"}),
            ToolCall("2", "write_file", {"file_path": "/b.txt", "content": "b\n"}),
        ]
    )
    code, out, _, _ = await _repl(
        tmp_path,
        [both, FakeProvider.text("wrote both files")],
        ["write a.txt and b.txt", "/exit"],
        out=console(no_color=True),
    )
    assert code == 0
    assert "write_file(/a.txt) · done" in out and "write_file(/b.txt) · done" in out


async def test_tool_progress_does_not_erase_prose_that_precedes_a_tool_call(tmp_path):
    """Prose and the tool label never share a terminal line (the old erase-line bug)."""
    prose = "Sure, I'll check that."
    reply = Message.assistant(
        [Text(prose), ToolCall("1", "write_file", {"file_path": "/notes.txt", "content": "hi\n"})]
    )
    code, out, _, _ = await _repl(
        tmp_path,
        [reply, FakeProvider.text("done")],
        ["check and write notes.txt", "/exit"],
        out=console(no_color=True),
    )
    assert code == 0 and prose in out
    prose_end = out.index(prose) + len(prose)
    label_idx = out.index("write_file(/notes.txt)")
    assert label_idx > prose_end and "\n" in out[prose_end:label_idx]


async def test_tool_progress_renders_error_line_when_user_rejects_approval(tmp_path):
    script = [
        FakeProvider.call("write_file", {"file_path": "/notes.txt", "content": "hi\n"}),
        FakeProvider.text("ok, not writing it"),
    ]
    code, out, _, _ = await _repl(
        tmp_path,
        script,
        ["write notes.txt please", "n", "not needed", "/exit"],
        cfg=_cfg(tmp_path),
        out=console(no_color=True),
    )
    assert code == 0
    assert "write_file(/notes.txt) · error" in out
    assert not (tmp_path / "notes.txt").exists()


async def test_always_allow_persists_a_rule_and_skips_the_next_prompt(tmp_path):
    script = [
        FakeProvider.call("write_file", {"file_path": "a.txt", "content": "1"}, "c1"),
        FakeProvider.call("write_file", {"file_path": "a.txt", "content": "2"}, "c2"),
        FakeProvider.text("done"),
    ]
    # "a" = always allow, "" = accept the suggested rule; the second write never asks
    code, out, _, _ = await _repl(
        tmp_path, script, ["write twice", "a", "", "/exit"], cfg=_cfg(tmp_path)
    )
    assert code == 0 and (tmp_path / "a.txt").read_text() == "2"
    assert "write_file:a.txt" in (tmp_path / ".luna" / "permissions.toml").read_text()


async def test_tool_progress_omits_content_dump_for_a_successful_read(tmp_path):
    (tmp_path / "notes.txt").write_text("first line of the file\nsecond line\n")
    script = [FakeProvider.call("read_file", {"file_path": "/notes.txt"}), FakeProvider.text("ok")]
    code, out, _, _ = await _repl(
        tmp_path, script, ["read notes.txt please", "/exit"], out=console(no_color=True)
    )
    assert code == 0
    assert "read_file(/notes.txt) · done" in out
    assert "first line of the file" not in out


async def test_answer_text_is_rendered_as_live_markdown(tmp_path):
    text = "**Done.** See the list:\n\n- one\n- two"
    out = Console(file=io.StringIO(), width=60, force_terminal=True, color_system="truecolor")
    code, rendered, _, _ = await _repl(
        tmp_path, [FakeProvider.text(text)], ["hi", "/exit"], out=out
    )
    assert code == 0
    assert "\x1b[1mDone.\x1b[0m" in rendered and "**Done.**" not in rendered
    assert "one" in rendered and "two" in rendered


async def test_repl_uses_the_shared_engine(monkeypatch, tmp_path):
    from luna.core import session as session_mod

    calls = []
    real = session_mod.engine.prepare_turn
    monkeypatch.setattr(
        session_mod.engine, "prepare_turn", lambda *a, **k: calls.append(a[1]) or real(*a, **k)
    )
    await _repl(tmp_path, [FakeProvider.text("ok")], ["hi"])  # input runs out -> EOF -> exit
    assert calls == ["hi"]


class _HangingProvider:
    """Streams one delta, then sends this process SIGINT and waits (until cancelled)."""

    name = "fake"

    def __init__(self):
        self.calls = 0

    async def stream(self, request):
        self.calls += 1
        if self.calls == 1:
            yield TextDelta("working")
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(30)
        async for event in FakeProvider([FakeProvider.text("second answer")]).stream(request):
            yield event


async def test_ctrl_c_cancels_the_turn_and_keeps_the_repl_alive(tmp_path):
    before = signal.getsignal(signal.SIGINT)
    cfg = _cfg(tmp_path, yolo=True)
    bundle = await build_agent(cfg, provider=_HangingProvider())
    out = console(no_color=True)
    log = open_log()
    session = Session.create(log, str(Path(tmp_path).resolve()))
    answers = iter(["long task", "again", "/exit"])
    code = await asyncio.wait_for(
        run_repl(
            bundle,
            console=out,
            input_fn=lambda _p: next(answers),
            log=log,
            session=session,
            config=cfg,
        ),
        timeout=10,
    )
    await bundle.aclose()
    text = out.file.getvalue()
    assert code == 0 and "turn cancelled" in text and "second answer" in text
    assert any(r.type == "cancelled" for r in session.records())
    assert signal.getsignal(signal.SIGINT) == before  # the REPL's handler is restored
