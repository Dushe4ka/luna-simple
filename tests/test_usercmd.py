def test_load_reads_project_commands(tmp_path):
    from luna.repl.usercmd import load

    d = tmp_path / ".luna" / "commands"
    d.mkdir(parents=True)
    (d / "review.md").write_text("---\ndescription: review a diff\n---\nReview: $ARGUMENTS\n")
    cmds = load(str(tmp_path))
    assert "review" in cmds
    assert cmds["review"].description == "review a diff"
    assert cmds["review"].body.strip() == "Review: $ARGUMENTS"


def test_project_overrides_user(tmp_path, isolated_config_home):
    from luna.config.config import config_dir
    from luna.repl.usercmd import load

    (config_dir() / "commands").mkdir(parents=True)
    (config_dir() / "commands" / "x.md").write_text("user version")
    d = tmp_path / ".luna" / "commands"
    d.mkdir(parents=True)
    (d / "x.md").write_text("project version")
    cmds = load(str(tmp_path))
    assert cmds["x"].body.strip() == "project version"


def test_malformed_frontmatter_is_skipped_gracefully(tmp_path):
    from luna.repl.usercmd import load

    d = tmp_path / ".luna" / "commands"
    d.mkdir(parents=True)
    (d / "ok.md").write_text("plain body, no frontmatter")
    cmds = load(str(tmp_path))
    assert cmds["ok"].body.strip() == "plain body, no frontmatter"
    assert cmds["ok"].description == ""


def test_load_skips_undecodable_files_gracefully(tmp_path):
    from luna.repl.usercmd import load

    d = tmp_path / ".luna" / "commands"
    d.mkdir(parents=True)
    (d / "bad.md").write_bytes(b"\xff\xfe\x00\x01binary garbage")
    (d / "ok.md").write_text("fine")
    cmds = load(str(tmp_path))
    assert "bad" not in cmds
    assert cmds["ok"].body.strip() == "fine"


def test_expand_substitutes_arguments(tmp_path):
    from luna.repl.usercmd import UserCommand, expand

    cmd = UserCommand(name="x", description="", body="do: $ARGUMENTS")
    assert expand(cmd, "the thing", str(tmp_path)) == "do: the thing"


def test_expand_runs_shell_injection(tmp_path):
    import sys

    from luna.repl.usercmd import UserCommand, expand

    cmd = UserCommand(name="x", description="", body=f"say: !`{sys.executable} -c \"print('hi')\"`")
    assert "hi" in expand(cmd, "", str(tmp_path))


def test_expand_leaves_file_mentions_for_the_caller(tmp_path):
    """@file/@agent resolution is the caller's job (run_repl), not usercmd.expand's —
    see the M2 fix in the final whole-branch review: expanding mentions here would
    corrupt an @agent-prefixed command body before run_repl's @agent check ever runs."""
    from luna.repl.usercmd import UserCommand, expand

    cmd = UserCommand(name="x", description="", body="look at @f.py")
    assert expand(cmd, "", str(tmp_path)) == "look at @f.py"
