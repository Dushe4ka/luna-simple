import sys


def test_detect_returns_empty_with_no_markers(tmp_path):
    from luna.turn.diagnose import detect

    assert detect(str(tmp_path)) == ""


def test_run_reports_command_output(tmp_path):
    from luna.turn.diagnose import run

    cmd = f"{sys.executable} -c \"print('a.py:3: undefined name x')\""
    text = run(cmd, str(tmp_path), ["a.py"])
    assert "a.py:3" in text


def test_run_clean_output_is_empty(tmp_path):
    from luna.turn.diagnose import run

    cmd = f'{sys.executable} -c "pass"'
    assert run(cmd, str(tmp_path), ["a.py"]) == ""


def test_run_caps_output_length(tmp_path):
    from luna.turn.diagnose import run

    cmd = f'{sys.executable} -c "[print(i) for i in range(200)]"'
    text = run(cmd, str(tmp_path), [])
    assert len(text.splitlines()) <= 40


def test_run_never_raises_on_a_bad_command(tmp_path):
    from luna.turn.diagnose import run

    assert run("this-command-does-not-exist-xyz", str(tmp_path), []) == ""


def test_run_disabled_when_command_empty(tmp_path):
    from luna.turn.diagnose import run

    assert run("", str(tmp_path), ["a.py"]) == ""
