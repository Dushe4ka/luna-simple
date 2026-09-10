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
