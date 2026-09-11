import sys


def test_detect_returns_empty_with_no_markers(tmp_path):
    from luna.fmt import detect

    assert detect(str(tmp_path)) == ""


def test_run_executes_and_reports_paths(tmp_path):
    from luna.fmt import run

    f = tmp_path / "a.txt"
    f.write_text("x")
    marker = tmp_path / "ran.txt"
    cmd = f"{sys.executable} -c \"open('{marker}', 'w').close()\""
    touched = run(cmd, str(tmp_path), ["a.txt"])
    assert marker.exists()
    assert touched == ["a.txt"]


def test_run_never_raises_on_a_bad_command(tmp_path):
    from luna.fmt import run

    assert run("this-command-does-not-exist-xyz", str(tmp_path), ["a.txt"]) == []


def test_run_with_empty_command_is_a_noop(tmp_path):
    from luna.fmt import run

    assert run("", str(tmp_path), ["a.txt"]) == []


def test_run_does_not_execute_shell_metacharacters_in_a_path(tmp_path):
    from luna.fmt import run

    marker = tmp_path / "PWNED"
    evil_path = f"x$(touch {marker}).py"
    # a formatter command that would just no-op on a nonexistent file — the point
    # is that shlex.quote must prevent the shell from ever seeing "$(...)" as a
    # command substitution
    cmd = f'{sys.executable} -c "import sys; sys.exit(0)"'
    run(cmd, str(tmp_path), [evil_path])
    assert not marker.exists()
