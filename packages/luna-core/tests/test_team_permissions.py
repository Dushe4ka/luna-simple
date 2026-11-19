"""The team permission profile: allow the plan's rules, deny the rest, deny rules win."""

from luna_core.team.permissions import DEFAULT_ALLOW, TeamPolicy, default_allow
from luna_core.tools import builtin_tools
from luna_core.workspace import Workspace

T = {t.name: t for t in builtin_tools()}


def make(tmp_path, allow=(), deny=(), file_writes=False):
    denied = []
    policy = TeamPolicy(
        allow,
        deny=deny,
        workspace=Workspace(tmp_path),
        member="dev",
        on_denied=lambda *a: denied.append(a),
        file_writes=file_writes,
    )
    return policy, denied


def test_commands_outside_the_profile_are_denied_and_recorded(tmp_path):
    policy, denied = make(tmp_path, allow=["execute:uv run pytest*"])
    assert policy.check(T["execute"], {"command": "uv run pytest -q"}).action == "allow"
    verdict = policy.check(T["execute"], {"command": "curl https://x"})
    assert verdict.action == "deny" and "permission profile" in verdict.message
    assert denied == [("dev", "execute", {"command": "curl https://x"}, "profile")]


def test_permissions_toml_deny_beats_the_profile(tmp_path):
    policy, denied = make(tmp_path, allow=["execute:git *"], deny=["execute:git push*"])
    assert policy.check(T["execute"], {"command": "git status"}).action == "allow"
    assert policy.check(T["execute"], {"command": "git push origin main"}).action == "deny"
    assert denied[-1][3] == "rule"


def test_file_writes_follow_the_task_kind(tmp_path):
    code, _ = make(tmp_path, file_writes=True)
    args = {"file_path": "a.py", "content": "x"}
    assert code.check(T["write_file"], args).action == "allow"
    research, denied = make(tmp_path)
    assert research.check(T["write_file"], args).action == "deny"
    assert denied[0][:2] == ("dev", "write_file")


def test_deny_rules_on_files_hold_even_for_code_tasks(tmp_path):
    policy, denied = make(tmp_path, deny=["write:.env"], file_writes=True)
    verdict = policy.check(T["write_file"], {"file_path": ".env", "content": "x"})
    assert verdict.action == "deny" and denied[0][3] == "rule"


def test_reads_are_allowed_and_never_reported(tmp_path):
    policy, denied = make(tmp_path)
    assert policy.check(T["read_file"], {"file_path": "a.py"}).action == "allow"
    assert denied == []


def test_child_keeps_the_profile(tmp_path):
    policy, denied = make(tmp_path, allow=["execute:git *"])
    child = policy.child(Workspace(tmp_path / "sub"))
    assert isinstance(child, TeamPolicy)
    assert child.check(T["execute"], {"command": "curl x"}).action == "deny"
    assert child.check(T["execute"], {"command": "git log"}).action == "allow"
    assert len(denied) == 1


def test_default_allow_adds_a_safe_verify_command():
    assert default_allow("") == DEFAULT_ALLOW
    assert default_allow("uv run  pytest -q") == (*DEFAULT_ALLOW, "execute:uv run pytest -q*")
    assert default_allow("bash -c 'make test'") == DEFAULT_ALLOW
