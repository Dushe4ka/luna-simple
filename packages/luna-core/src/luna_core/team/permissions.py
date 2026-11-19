"""The permission profile of a team run: allow what the plan lists, deny the rest.

Nobody is asked during a run. A call the profile does not allow is denied
and reported through ``on_denied`` (it becomes a ``team_denied`` event and a
line in the report). Deny rules from ``permissions.toml`` always win.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from luna_core.policy import (
    MUTATING_FILE_TOOLS,
    Policy,
    Rules,
    UnsafeRule,
    Verdict,
    check_rule_safe,
)
from luna_core.tools.base import Tool, args_preview
from luna_core.workspace import Workspace

#: The profile a plan accepted unseen (``--yes``) starts from.
DEFAULT_ALLOW = (
    "execute:git status*",
    "execute:git diff*",
    "execute:git log*",
    "execute:git show*",
)
_RULE_DENY = "blocked by a Luna permission rule"

#: ``(member, tool, args, reason)`` with reason ``"profile"`` or ``"rule"``.
Denied = Callable[[str, str, dict, str], None]


def default_allow(verify_command: str = "") -> tuple[str, ...]:
    """Read-only git plus the verify command (when it is safe as an allow rule)."""
    command = " ".join(verify_command.split())
    if not command:
        return DEFAULT_ALLOW
    rule = f"execute:{command}*"
    try:
        check_rule_safe(rule)
    except UnsafeRule:
        return DEFAULT_ALLOW
    return (*DEFAULT_ALLOW, rule)


class TeamPolicy(Policy):
    """A policy that never asks a human.

    ``allow`` is the run's profile, ``deny`` the user's deny rules. With
    ``file_writes`` (a ``code`` task in its own worktree) file edits are
    allowed; everything else that would need approval is denied.
    """

    def __init__(
        self,
        allow: Iterable[str],
        *,
        deny: Iterable[str] = (),
        workspace: Workspace,
        member: str,
        on_denied: Denied,
        file_writes: bool = False,
    ) -> None:
        self.allow_rules = tuple(allow)
        super().__init__(Rules(allow=list(self.allow_rules), deny=list(deny)), workspace=workspace)
        self.member = member
        self.on_denied = on_denied
        self.file_writes = file_writes

    def check(self, tool: Tool, args: dict) -> Verdict:
        """Allow, or deny and report; never ask."""
        verdict = super().check(tool, args)
        if verdict.action == "deny":
            if verdict.message.startswith(_RULE_DENY):
                self.on_denied(self.member, tool.name, dict(args), "rule")
            return verdict
        if verdict.action != "ask":
            return verdict
        if self.file_writes and tool.name in MUTATING_FILE_TOOLS:
            return Verdict("allow")
        self.on_denied(self.member, tool.name, dict(args), "profile")
        return Verdict(
            "deny",
            f"{tool.name} ({args_preview(args, 80)}) is outside the team's permission profile "
            "and was not run; do the task without it or report it blocked with board_update",
        )

    def child(self, workspace: Workspace | None = None) -> TeamPolicy:
        """Return a policy with the same profile (members do not delegate)."""
        return TeamPolicy(
            self.allow_rules,
            deny=self.rules.deny,
            workspace=workspace or self.workspace,
            member=self.member,
            on_denied=self.on_denied,
            file_writes=self.file_writes,
        )
