"""Git plumbing for a team run: the team branch, member worktrees, serial merges.

The user's working tree and branch are never touched. The team branch
``luna/team/<id>`` starts at ``HEAD`` and lives checked out in its own
integration worktree; each task attempt gets a worktree on
``luna/teamwork/<id>/<task>`` from the team tip. The runner serialises
``merge`` calls.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from luna_core.errors import LunaCoreError
from luna_core.shell import run_sync
from luna_core.subagents import (
    _IDENTITY,
    _git,
    _git_toplevel,
    _prune_worktrees,
    _worktree_lock,
    finish_worktree,
)

_MARKERS = r"^(<{7}|>{7})( |$)"


class TeamGitError(LunaCoreError):
    """A git step of a team run failed."""


@dataclass(frozen=True)
class MemberTree:
    """The worktree of one task attempt."""

    path: Path
    branch: str
    base: str


@dataclass(frozen=True)
class MergeResult:
    """How merging a task branch into the team branch went."""

    status: str  # "merged" | "empty" | "conflict" | "markers"
    detail: str = ""
    commit: str = ""
    before: str = ""


def team_branch(run_id: str) -> str:
    """Return the run's result branch."""
    return f"luna/team/{run_id}"


def task_branch(run_id: str, task_id: str) -> str:
    """Return the branch of a task's attempts."""
    return f"luna/teamwork/{run_id}/{task_id}"


class TeamGit:
    """Git operations of one run in the repository at ``root``."""

    def __init__(self, root: str | Path, run_id: str) -> None:
        self.root = Path(root).resolve()
        self.run_id = run_id
        self.branch = team_branch(run_id)
        self.integration = self.root / ".luna" / "worktrees" / f"team-{run_id}"

    @staticmethod
    def _ok(proc: subprocess.CompletedProcess, what: str) -> str:
        if proc.returncode != 0:
            raise TeamGitError(f"{what}: {(proc.stderr or proc.stdout).strip()}")
        return proc.stdout.strip()

    def has_branch(self, name: str) -> bool:
        """Return True when the branch exists."""
        return _git(self.root, "rev-parse", "--verify", "-q", name).returncode == 0

    def start(self) -> str:
        """Create the team branch at ``HEAD`` and its integration worktree; return ``HEAD``."""
        top = _git_toplevel(self.root)
        if top is None or top != self.root:
            raise TeamGitError("a team run needs the project root to be a git repository root")
        with _worktree_lock(self.root):
            head = _git(self.root, "rev-parse", "--verify", "-q", "HEAD").stdout.strip()
            if not head:
                raise TeamGitError("the repository has no commits yet; commit once first")
            if self.has_branch(self.branch):
                raise TeamGitError(f"branch {self.branch} already exists")
            self._ok(_git(self.root, "branch", self.branch, head), "git branch")
            self._attach()
        return head

    def _attach(self) -> None:
        _prune_worktrees(self.root)
        if (self.integration / ".git").exists():
            return
        self.integration.parent.mkdir(parents=True, exist_ok=True)
        self._ok(
            _git(self.root, "worktree", "add", "-q", str(self.integration), self.branch),
            "git worktree add",
        )

    def ensure(self) -> None:
        """Re-attach the integration worktree (after a restart or a close)."""
        with _worktree_lock(self.root):
            self._attach()

    def tip(self) -> str:
        """Return the team branch's current commit."""
        return self._ok(_git(self.root, "rev-parse", self.branch), "git rev-parse")

    def member_tree(
        self, task_id: str, attempt: int, *, branch: str | None = None, merge: str | None = None
    ) -> MemberTree:
        """Create a worktree for an attempt: ``branch`` if it exists, else a new one at the tip.

        With ``merge`` the new worktree starts merging that branch and leaves
        its conflicts in the files for the member to resolve.
        """
        path = self.root / ".luna" / "worktrees" / f"team-{self.run_id}-{task_id}-{attempt}"
        with _worktree_lock(self.root):
            _prune_worktrees(self.root)
            path.parent.mkdir(parents=True, exist_ok=True)
            if branch and self.has_branch(branch):
                self._ok(
                    _git(self.root, "worktree", "add", "-q", str(path), branch), "git worktree add"
                )
                base = self._ok(_git(self.root, "merge-base", branch, self.branch), "merge-base")
                return MemberTree(path, branch, base)
            name = task_branch(self.run_id, task_id)
            if self.has_branch(name):
                _git(self.root, "branch", "-D", name)
            tip = self.tip()
            self._ok(
                _git(self.root, "worktree", "add", "-q", "-b", name, str(path), tip),
                "git worktree add",
            )
        if merge:
            # Exits non-zero on conflicts: that is the point, the member resolves them.
            _git(
                path, "-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-commit", merge,
                env=_IDENTITY,
            )  # fmt: skip
        return MemberTree(path, name, tip)

    def finish(self, tree: MemberTree, message: str) -> tuple[str, list[str]]:
        """Commit the attempt and drop its worktree; return (diffstat, its commits)."""
        stat, _diff = finish_worktree(self.root, tree.path, tree.branch, tree.base, message)
        if not stat:
            # A retry that undid its own earlier work leaves a branch with nothing to merge.
            if self.has_branch(tree.branch):
                _git(self.root, "branch", "-D", tree.branch)
            return "", []
        listed = _git(self.root, "rev-list", "--reverse", f"{tree.base}..{tree.branch}")
        return stat, self._ok(listed, "git rev-list").split()

    def markers(self, branch: str) -> list[str]:
        """Files changed on ``branch`` that still hold conflict markers."""
        changed = _git(self.root, "diff", "--name-only", f"{self.branch}...{branch}").stdout
        names = [n for n in changed.splitlines() if n]
        if not names:
            return []
        found = _git(self.root, "grep", "-l", "-E", _MARKERS, branch, "--", *names).stdout
        return sorted({line.split(":", 1)[1] for line in found.splitlines() if ":" in line})

    def merge(self, branch: str, message: str) -> MergeResult:
        """Merge ``branch`` into the team branch (the caller serialises merges)."""
        marked = self.markers(branch)
        if marked:
            return MergeResult("markers", ", ".join(marked))
        before = self.tip()
        proc = _git(
            self.integration, "-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-verify",
            "-m", message, branch, env=_IDENTITY,
        )  # fmt: skip
        if proc.returncode != 0:
            _git(self.integration, "merge", "--abort")
            detail = (proc.stdout + proc.stderr).strip()[-2000:]
            return MergeResult("conflict", detail, before=before)
        after = self.tip()
        return MergeResult("merged" if after != before else "empty", commit=after, before=before)

    def undo_merge(self, before: str) -> None:
        """Move the team branch back to ``before`` (a merge that failed verification)."""
        self._ok(_git(self.integration, "reset", "--hard", "-q", before), "git reset")

    def verify(self, command: str, timeout: float) -> tuple[bool, str]:
        """Run the verify command on the team branch; return (passed, output tail)."""
        try:
            proc = run_sync(command, cwd=self.integration, timeout=timeout)
        except OSError as exc:
            return False, str(exc)
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output[-4000:]

    def delete_branch(self, name: str) -> None:
        """Delete a task branch (merged or abandoned)."""
        _git(self.root, "branch", "-D", name)

    def diffstat(self, base: str) -> str:
        """``git diff --stat`` of the whole run."""
        return _git(self.root, "diff", "--stat", base, self.branch).stdout.strip()

    def salvage(self, path: Path, branch: str) -> str:
        """Commit and drop a worktree left by an interrupted attempt; return its diffstat."""
        if not path.exists():
            _git(self.root, "worktree", "prune")
            return ""
        base = self._ok(_git(self.root, "merge-base", branch, self.branch), "git merge-base")
        stat, _diff = finish_worktree(self.root, path, branch, base, "luna team: interrupted work")
        return stat

    def close(self) -> None:
        """Remove the integration worktree; the team branch stays."""
        with _worktree_lock(self.root):
            _git(self.root, "worktree", "remove", "--force", str(self.integration))
            _git(self.root, "worktree", "prune")
