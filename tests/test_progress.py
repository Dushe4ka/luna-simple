import io

from rich.console import Console

from luna.ui.progress import ToolProgress, _pulse_color, _summarize_call


def _console():
    return Console(file=io.StringIO(), force_terminal=True, no_color=True)


def test_summarize_call_for_file_tools_shows_the_path():
    for name in ("write_file", "edit_file", "read_file", "delete"):
        assert _summarize_call(name, {"file_path": "/a/b.py"}) == f"{name}(/a/b.py)"


def test_summarize_call_for_execute_shows_and_truncates_the_command():
    assert _summarize_call("execute", {"command": "ls -la"}) == "execute(ls -la)"
    long_cmd = "x" * 80
    result = _summarize_call("execute", {"command": long_cmd})
    assert result == f"execute({'x' * 60}…)"


def test_summarize_call_for_glob_and_grep_shows_the_pattern():
    assert _summarize_call("glob", {"pattern": "**/*.py"}) == "glob(**/*.py)"
    assert _summarize_call("grep", {"pattern": "TODO"}) == "grep(TODO)"


def test_summarize_call_for_task_shows_the_subagent_type():
    args = {"subagent_type": "researcher", "description": "look around"}
    assert _summarize_call("task", args) == "task(researcher)"


def test_summarize_call_falls_back_to_bare_name_on_missing_args():
    assert _summarize_call("write_file", {}) == "write_file"
    assert _summarize_call("execute", {}) == "execute"
    assert _summarize_call("task", {}) == "task"


def test_summarize_call_for_unknown_tool_is_just_the_name():
    assert _summarize_call("ls", {"path": "/x"}) == "ls"
    assert _summarize_call("manage_mcp", {"action": "list"}) == "manage_mcp"


def test_pulse_color_is_deterministic_for_a_fixed_clock_value():
    a = _pulse_color(1.0, 0)
    b = _pulse_color(1.0, 0)
    assert a == b
    # different dots at the same instant are (almost always) out of phase
    assert _pulse_color(1.0, 0) != _pulse_color(1.0, 1)


def test_tool_progress_tracks_pending_count():
    progress = ToolProgress(_console(), clock=lambda: 0.0)
    assert progress.pending_count() == 0
    progress.start("1", "write_file", {"file_path": "/a.py"})
    assert progress.pending_count() == 1
    progress.start("2", "execute", {"command": "ls"})
    assert progress.pending_count() == 2
    progress.finish("1", True, "done")
    assert progress.pending_count() == 1
    progress.finish("2", True, "")
    assert progress.pending_count() == 0
    progress.close()


def test_tool_progress_finish_prints_a_permanent_detail_line_including_the_label():
    """The label (tool + its key arg) must appear in the PERMANENT done line,
    not only in the transient pending animation. Rich's ``Live.start()``
    defaults to ``refresh=False`` — its first frame only renders on the
    background thread's first tick (~100ms at the default refresh rate) or
    on ``stop()``, which by then has already popped the finished entry out
    of the pending dict. A call that starts and finishes faster than one
    tick (true of every call in this synchronous test suite, and of many
    real fast calls like `ls`) would otherwise show its label nowhere at
    all — this test guards against exactly that regression."""
    console = _console()
    progress = ToolProgress(console, clock=lambda: 0.0)
    progress.start("1", "write_file", {"file_path": "/a.py"})
    progress.finish("1", True, "Updated file /a.py")
    progress.close()
    out = console.file.getvalue()
    assert "write_file(/a.py) · done" in out
    assert "Updated file /a.py" in out


def test_tool_progress_finish_for_task_uses_in_wording_and_integer_seconds():
    ticks = iter([0.0, 0.0, 14.3])
    console = _console()
    progress = ToolProgress(console, clock=lambda: next(ticks))
    progress.start("1", "task", {"subagent_type": "researcher", "description": "look around"})
    progress.finish("1", True, "found 3 files")
    progress.close()
    out = console.file.getvalue()
    assert "task(researcher) · done in 14s · found 3 files" in out


def test_tool_progress_finish_for_a_failed_call_says_error():
    console = _console()
    progress = ToolProgress(console, clock=lambda: 0.0)
    progress.start("1", "execute", {"command": "false"})
    progress.finish("1", False, "exit code 1")
    progress.close()
    out = console.file.getvalue()
    assert "execute(false) · error" in out
    assert "exit code 1" in out


def test_tool_progress_finish_on_unknown_id_is_a_no_op():
    console = _console()
    progress = ToolProgress(console, clock=lambda: 0.0)
    progress.finish("never-started", True, "x")  # must not raise
    progress.close()
    assert console.file.getvalue() == ""


def test_tool_progress_pause_and_resume_do_not_lose_pending_state():
    progress = ToolProgress(_console(), clock=lambda: 0.0)
    progress.start("1", "write_file", {"file_path": "/a.py"})
    progress.pause()
    assert progress.pending_count() == 1
    progress.resume()
    assert progress.pending_count() == 1
    progress.finish("1", True, "")
    progress.close()


def test_render_pending_shows_the_label_and_a_second_line_for_task_description():
    progress = ToolProgress(_console(), clock=lambda: 0.0)
    progress.start("1", "task", {"subagent_type": "researcher", "description": "look around"})
    rendered = progress._render_pending().plain
    assert "task(researcher)" in rendered
    assert "look around" in rendered
    progress.finish("1", True, "")
    progress.close()
