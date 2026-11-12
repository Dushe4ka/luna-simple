"""Execute rules on Windows: ``.exe``, case and program paths cannot bypass them."""

import pytest
from luna_core import shell
from luna_core.policy import Policy, Rules, UnsafeRule, check_rule_safe, suggest_rules
from luna_core.shellsplit import program_name


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(shell, "IS_WINDOWS", True)


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(shell, "IS_WINDOWS", False)


DENY_RM = Rules(deny=["execute:rm *"], allow=["execute:ls *", "execute:git *"])


@pytest.mark.parametrize(
    "command",
    [
        "RM -rf x",
        "rm.exe -rf x",
        "Rm.EXE -rf x",
        "C:/Git/usr/bin/rm.exe -rf x",
        "/usr/bin/rm -rf x",
        "ENV.EXE rm -rf x",
        'BASH.EXE -c "rm -rf x"',
        "ls && RM.exe x",
        r"'C:\Git\usr\bin\rm.exe' -rf x",
        r'"C:\\Git\\usr\\bin\\rm.exe" -rf x',
        r'"C:\Git\usr\bin\rm.exe" -rf x',
        r"C:\\Git\\usr\\bin\\rm.exe -rf x",
        r"'C:\Git\usr\bin\RM' x",
    ],
)
def test_deny_rm_cannot_be_bypassed_on_windows(windows, command):
    assert DENY_RM.match("execute", {"command": command}) == "deny"


def test_posix_keeps_case_sensitive_program_names(posix):
    # on Linux/macOS `RM` is a different program; only the path is stripped there
    assert program_name("/usr/bin/rm") == "rm"
    assert program_name("RM") == "RM"


def test_program_name_on_windows(windows):
    assert program_name("C:/Git/usr/bin/RM.EXE") == "rm"
    assert program_name("npm.cmd") == "npm"


def test_allow_keeps_the_path_so_a_local_script_is_not_the_real_tool(windows):
    assert DENY_RM.match("execute", {"command": "./ls.exe -la"}) is None
    assert DENY_RM.match("execute", {"command": "GIT.exe status"}) == "allow"


@pytest.mark.parametrize(
    "runner",
    ["cmd", "cmd.exe", "powershell", "pwsh", "wsl", "start", "cygstart", "winpty"],
)
def test_windows_runners_are_never_allowed(windows, runner):
    with pytest.raises(UnsafeRule):
        check_rule_safe(f"execute:{runner} *")
    assert suggest_rules("execute", {"command": f"{runner} /c del x"}) == ()


@pytest.mark.parametrize("runner", ["cmd", "cmd.exe", "pwsh", "wsl"])
def test_windows_runner_rules_are_refused_on_every_os(runner):
    with pytest.raises(UnsafeRule):
        check_rule_safe(f"execute:{runner} *")


def test_backslash_path_is_never_allowed(windows):
    for command in (r"'C:\Git\usr\bin\git.exe' status", r"C:\\Git\\git.exe status"):
        assert DENY_RM.match("execute", {"command": command}) is None


@pytest.mark.parametrize("command", ["rm -rf x", "RM.EXE x", "C:/x/rm x"])
def test_deny_pattern_with_exe_is_folded_on_windows(windows, command):
    assert Rules(deny=["execute:rm.exe *"]).match("execute", {"command": command}) == "deny"


def test_ask_pattern_with_exe_is_folded_on_windows(windows):
    rules = Rules(ask=["execute:Git.EXE *"])
    assert rules.match("execute", {"command": "git push"}) == "ask"


def test_deny_pattern_with_exe_is_literal_on_posix(posix):
    assert Rules(deny=["execute:rm.exe *"]).match("execute", {"command": "rm x"}) is None


@pytest.mark.parametrize("fold_case", [True, False])
@pytest.mark.parametrize("subject", ["secrets/a.key", r"secrets\a.key", "SECRETS/A.KEY"])
def test_backslash_path_rule_still_matches_with_case_folding(windows, fold_case, subject):
    """Final review I4: on Windows ``fold_case`` is always on, and ``fnmatchcase``
    does not treat ``\\`` as a separator, so ``read_file:secrets\\*.key`` stopped
    matching the POSIX-form subjects the workspace hands out."""
    rules = Rules(deny=[r"read_file:secrets\*.key"], fold_case=fold_case)
    if not fold_case and subject.isupper():
        pytest.skip("case-sensitive matching is not what this test is about")
    assert rules.match("read_file", {"file_path": subject}) == "deny"


def test_backslash_is_a_plain_character_on_posix(posix):
    rules = Rules(deny=[r"read_file:secrets\*.key"], fold_case=True)
    assert rules.match("read_file", {"file_path": "secrets/a.key"}) is None


WINDOWS_RUNNER_LINES = [
    "cmd //c rm -rf x",
    "cmd.exe /c rm -rf x",
    "CMD /K rm x",
    "wsl rm -rf x",
    "powershell -c 'rm -rf x'",
    'pwsh.exe -Command "rm -rf x"',
    "winpty rm -rf x",
    "start rm -rf x",
    "cygstart rm x",
    "ls && cmd //c rm x",
    "env cmd //c rm x",
    '"C:/Windows/System32/cmd.exe" /c rm x',
]


@pytest.mark.parametrize("command", WINDOWS_RUNNER_LINES)
def test_windows_runners_are_asked_about_when_execute_has_deny_rules(windows, command):
    """Final review minor 1: deny rules do not look inside ``cmd /c`` / ``wsl`` /
    ``powershell -c`` payloads, so with a deny rule these lines are always asked
    about — never allowed by a rule or by yolo."""
    assert DENY_RM.match("execute", {"command": command}) == "ask"


def test_windows_runner_ask_holds_in_yolo(windows):
    from luna_core.tools.shell import execute

    policy = Policy(Rules(deny=["execute:rm *"]), yolo=True)
    assert policy.check(execute, {"command": "cmd //c rm -rf x"}).action == "ask"


def test_windows_runners_without_deny_rules_are_unchanged(windows):
    assert Rules(allow=["execute:ls *"]).match("execute", {"command": "cmd //c dir"}) is None


def test_windows_runner_rule_does_not_touch_posix(posix):
    assert DENY_RM.match("execute", {"command": "wsl rm -rf x"}) is None
