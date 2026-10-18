from luna.core.permissions import RuleSet, append_project_rule, load_rules, suggest_rule


def test_match_execute_prefix():
    rs = RuleSet(allow=["execute:git status*"], deny=["execute:git push*"])
    assert rs.match("execute", {"command": "git status -s"}) == "allow"
    assert rs.match("execute", {"command": "git push origin"}) == "deny"
    assert rs.match("execute", {"command": "ls"}) is None


def test_match_path_glob():
    rs = RuleSet(allow=[], deny=["write_file:.env", "write_file:secrets/*"])
    assert rs.match("write_file", {"file_path": ".env"}) == "deny"
    assert rs.match("write_file", {"file_path": "secrets/k.txt"}) == "deny"
    assert rs.match("write_file", {"file_path": "app.py"}) is None


def test_match_virtual_rooted_path():
    # LocalShellBackend(virtual_mode=True) hands tools "/"-rooted paths.
    rs = RuleSet(deny=["write_file:.env", "write_file:secrets/*"])
    assert rs.match("write_file", {"file_path": "/.env"}) == "deny"
    assert rs.match("write_file", {"file_path": "/secrets/k.txt"}) == "deny"
    # A rule written with a leading slash must also match a relative call.
    rs2 = RuleSet(deny=["write_file:/.env"])
    assert rs2.match("write_file", {"file_path": ".env"}) == "deny"


def test_load_rules_ignores_wrong_shape(tmp_path):
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text(
        'allow = "not-a-list"\ndeny = [1, "write_file:.env"]\n'
    )
    rs = load_rules(str(tmp_path))
    assert rs.allow == []
    assert rs.deny == ["write_file:.env"]


def test_deny_beats_allow():
    rs = RuleSet(allow=["execute:*"], deny=["execute:rm -rf*"])
    assert rs.match("execute", {"command": "rm -rf /"}) == "deny"


def test_load_merges_sources(tmp_path, isolated_config_home):
    (isolated_config_home / "luna").mkdir(parents=True)
    (isolated_config_home / "luna" / "config.toml").write_text(
        '[permissions]\nallow = ["execute:a*"]\n'
    )
    (tmp_path / ".luna").mkdir()
    (tmp_path / ".luna" / "permissions.toml").write_text('deny = ["execute:b*"]\n')
    rs = load_rules(str(tmp_path))
    assert "execute:a*" in rs.allow and "execute:b*" in rs.deny


def test_append_project_rule(tmp_path):
    append_project_rule(str(tmp_path), "execute:npm test *")
    rs = load_rules(str(tmp_path))
    assert "execute:npm test *" in rs.allow


def test_suggest_rule():
    assert suggest_rule("execute", {"command": "pytest -q"}, ".") == "execute:pytest *"
    assert suggest_rule("write_file", {"file_path": "a/b.py"}, ".") == "write_file:a/b.py"


def test_wildcard_tool_blocks_every_tool():
    rs = RuleSet(deny=["*:.env"])
    assert rs.match("write_file", {"file_path": "/.env"}) == "deny"
    assert rs.match("edit_file", {"file_path": ".env"}) == "deny"
    assert rs.match("delete", {"file_path": ".env"}) == "deny"
    assert rs.match("read_file", {"file_path": "app.py"}) is None


def test_write_group_blocks_mutators_only():
    rs = RuleSet(deny=["write:secrets/*"])
    assert rs.match("write_file", {"file_path": "secrets/k.txt"}) == "deny"
    assert rs.match("edit_file", {"file_path": "secrets/k.txt"}) == "deny"
    assert rs.match("delete", {"file_path": "secrets/k.txt"}) == "deny"
    assert rs.match("read_file", {"file_path": "secrets/k.txt"}) is None


def test_fs_group_includes_read():
    assert RuleSet(deny=["fs:x.txt"]).match("read_file", {"file_path": "x.txt"}) == "deny"


def test_wildcard_matches_execute_by_command():
    rs = RuleSet(deny=["*:git push*"])
    assert rs.match("execute", {"command": "git push origin"}) == "deny"
    assert rs.match("execute", {"command": "git status"}) is None


def test_exact_tool_rules_unchanged():
    rs = RuleSet(deny=["write_file:.env"], allow=["execute:pytest*"])
    assert rs.match("write_file", {"file_path": "/.env"}) == "deny"
    assert rs.match("edit_file", {"file_path": ".env"}) is None  # exact tool only
    assert rs.match("execute", {"command": "pytest -q"}) == "allow"
