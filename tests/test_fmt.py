import sys


def test_detect_returns_empty_with_no_markers(tmp_path):
    from luna.turn.fmt import detect

    assert detect(str(tmp_path)) == ""


def test_run_executes_and_reports_paths(tmp_path):
    from luna.turn.fmt import run

    f = tmp_path / "a.txt"
    f.write_text("x")
    marker = tmp_path / "ran.txt"
    cmd = f"{sys.executable} -c \"open('{marker}', 'w').close()\""
    touched = run(cmd, str(tmp_path), ["a.txt"])
    assert marker.exists()
    assert touched == ["a.txt"]


def test_run_never_raises_on_a_bad_command(tmp_path):
    from luna.turn.fmt import run

    assert run("this-command-does-not-exist-xyz", str(tmp_path), ["a.txt"]) == []


def test_run_with_empty_command_is_a_noop(tmp_path):
    from luna.turn.fmt import run

    assert run("", str(tmp_path), ["a.txt"]) == []


def test_run_does_not_execute_shell_metacharacters_in_a_path(tmp_path):
    from luna.turn.fmt import run

    marker = tmp_path / "PWNED"
    evil_path = f"x$(touch {marker}).py"
    # a formatter command that would just no-op on a nonexistent file — the point
    # is that shlex.quote must prevent the shell from ever seeing "$(...)" as a
    # command substitution
    cmd = f'{sys.executable} -c "import sys; sys.exit(0)"'
    run(cmd, str(tmp_path), [evil_path])
    assert not marker.exists()


def test_run_prefixes_paths_so_a_dash_prefixed_filename_is_never_read_as_an_option(tmp_path):
    """Regression for NEW-4 (round 2 of the final review): shlex.quote only stops
    the shell from treating a path as metacharacters — it does nothing to stop the
    INVOKED tool's own argument parser from reading a literal ``--version`` or
    ``-h``-named file as a flag rather than a positional path. ``run`` must prefix
    every path with ``./`` so it can never be mistaken for an option, even by a
    real argparse-style CLI.
    """
    from luna.turn.fmt import run

    stub = tmp_path / "toolstub.py"
    stub.write_text(
        "import argparse, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('paths', nargs='*')\n"
        "args = p.parse_args()\n"
        "sys.exit(0 if args.paths == ['./--version'] else 1)\n"
    )
    cmd = f"{sys.executable} {stub}"
    # a file literally named "--version" — argparse would otherwise choke on it
    # as an unrecognized option (nonzero exit) instead of treating it as a path
    touched = run(cmd, str(tmp_path), ["--version"])
    assert touched == ["--version"]
