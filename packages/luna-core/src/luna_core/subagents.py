"""Subagents: delegated child runs, concurrently, optionally in git worktrees."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import shutil
import subprocess
import threading
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from luna_core.context import build_system_prompt
from luna_core.errors import LunaCoreError
from luna_core.events import Cancelled, NeedsApproval, Notice, ToolStarted, TurnError
from luna_core.filelock import file_lock
from luna_core.loop import Agent
from luna_core.session import Session
from luna_core.undo import _snapshot_tree
from luna_core.workspace import Workspace

READ_ONLY = ("ls", "read_file", "glob", "grep")
WRITE_TOOLS = ("write_file", "edit_file", "delete", "execute")
VALID_TOOLS = frozenset((*READ_ONLY, *WRITE_TOOLS, "write_todos"))
ISOLATION = ("auto", "none", "worktree")
_IDENTITY = {
    "GIT_AUTHOR_NAME": "Luna",
    "GIT_AUTHOR_EMAIL": "luna@localhost",
    "GIT_COMMITTER_NAME": "Luna",
    "GIT_COMMITTER_EMAIL": "luna@localhost",
}
_DIFF_LIMIT = 20_000
# git refuses concurrent worktree bookkeeping; parallel subagents take turns here,
# and other processes on the same repository (REPL + server) through an OS lock
# file in the git common directory (see ``_worktree_lock``).
_GIT_LOCK = threading.Lock()


def _worktree_lock_path(root: Path) -> Path:
    """Lock file for worktree bookkeeping: ``<git common dir>/luna-worktrees.lock``.

    It lives outside the working tree so it never shows up in snapshots,
    ``git status`` or the ``.luna/worktrees`` holder that pruning sweeps.
    """
    try:
        proc = _git(root, "rev-parse", "--git-common-dir")
        common = proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        common = ""
    return (root / (common or ".git")).resolve() / "luna-worktrees.lock"


@contextlib.contextmanager
def _worktree_lock(root: Path):
    """Serialise worktree create/prune/finish across threads and processes."""
    with _GIT_LOCK, file_lock(_worktree_lock_path(root)):
        yield


class SubagentConfigError(LunaCoreError):
    """A subagent definition is invalid."""


@dataclass(frozen=True)
class SubagentSpec:
    """A subagent definition."""

    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] = READ_ONLY
    model: str | None = None
    unsafe: bool = False
    isolation: str = "auto"
    max_steps: int = 30
    max_cost: float | None = None

    @property
    def writes(self) -> bool:
        """True when it may change files or run commands."""
        return bool(set(self.tools) & set(WRITE_TOOLS)) or self.unsafe


BUILTIN_SUBAGENTS: dict[str, SubagentSpec] = {
    "researcher": SubagentSpec(
        "researcher",
        "Investigate the codebase, docs or a question and report back. Read-only.",
        "You explore and report. You never modify files or run mutating commands. "
        "End with a concise report: findings, evidence (paths and lines), open questions.",
    ),
    "reviewer": SubagentSpec(
        "reviewer",
        "Review a diff or files for bugs, risks and simplifications. Read-only.",
        "You review code critically and return concrete, prioritised findings "
        "with paths and lines.",
    ),
    "general-purpose": SubagentSpec(
        "general-purpose",
        "Do a self-contained multi-step task end to end; "
        "may edit files (in its own git worktree when possible).",
        "You complete the delegated task end to end, verify it (tests when they exist), "
        "and finish with a short report of what changed and what is left.",
        tools=(*READ_ONLY, *WRITE_TOOLS, "write_todos"),
        unsafe=True,
    ),
}


def _check_types(path: Path, name: str, cfg: dict) -> None:
    """Reject wrong-typed values of known keys, naming the file and the key."""
    checks: dict[str, tuple[type | tuple[type, ...], str]] = {
        "description": (str, "a string"),
        "prompt": (str, "a string"),
        "model": (str, "a string"),
        "unsafe": (bool, "a boolean (unsafe = true or unsafe = false)"),
        "isolation": (str, "a string"),
        "max_steps": (int, "an integer"),
        "max_cost": ((int, float), "a number"),
    }
    for key, (kind, label) in checks.items():
        if key not in cfg:
            continue
        value = cfg[key]
        # ``bool`` is an ``int`` in Python; a TOML ``true`` is not a step count.
        wrong = not isinstance(value, kind) or (kind is not bool and isinstance(value, bool))
        if wrong:
            raise SubagentConfigError(
                f"{path}: [subagent.{name}] {key} must be {label}, got {value!r}"
            )
    tools = cfg.get("tools", READ_ONLY)
    if not isinstance(tools, (list, tuple)) or not all(isinstance(t, str) for t in tools):
        raise SubagentConfigError(
            f"{path}: [subagent.{name}] tools must be a list of strings, got {tools!r}"
        )


def load_subagents(
    files: list[Path], *, on_warn: Callable[[str], None] | None = None
) -> dict[str, SubagentSpec]:
    """Built-in subagents overridden/extended by ``[subagent.<name>]`` tables in ``files``."""
    specs = dict(BUILTIN_SUBAGENTS)
    for path in files:
        try:
            with Path(path).open("rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SubagentConfigError(f"cannot read {path}: {exc}") from exc
        table = data.get("subagent") or {}
        if not isinstance(table, dict):
            raise SubagentConfigError(f"{path}: 'subagent' must be a table of [subagent.<name>]")
        for name, cfg in table.items():
            if not isinstance(cfg, dict):
                raise SubagentConfigError(f"{path}: [subagent.{name}] must be a table")
            _check_types(path, name, cfg)
            tools = tuple(cfg.get("tools", READ_ONLY))
            unknown = set(tools) - VALID_TOOLS
            if unknown:
                raise SubagentConfigError(
                    f"subagent {name!r}: unknown tools {sorted(unknown)}; "
                    f"valid: {', '.join(sorted(VALID_TOOLS))}"
                )
            mutating = set(tools) & set(WRITE_TOOLS)
            if mutating and cfg.get("unsafe") is not True:
                raise SubagentConfigError(
                    f"subagent {name!r} requests {sorted(mutating)}; add 'unsafe = true' to "
                    f"[subagent.{name}] to allow it to change the repository"
                )
            if mutating and on_warn:
                on_warn(
                    f"subagent {name!r} may change files or run commands "
                    "(rules, approvals and undo still apply)"
                )
            isolation = cfg.get("isolation", "auto")
            if isolation not in ISOLATION:
                raise SubagentConfigError(
                    f"subagent {name!r}: isolation must be one of {ISOLATION}"
                )
            specs[name] = SubagentSpec(
                name,
                str(cfg.get("description", name)),
                str(cfg.get("prompt", f"You are the {name} subagent.")),
                tools,
                cfg.get("model"),
                bool(cfg.get("unsafe", False)),
                isolation,
                int(cfg.get("max_steps", 30)),
                float(cfg["max_cost"]) if "max_cost" in cfg else None,
            )
    return specs


def _git(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(env or {})},
    )


def _git_toplevel(root: Path) -> Path | None:
    """Return the repository's top-level directory, or None outside git."""
    try:
        proc = _git(root, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip()).resolve()


def create_worktree(root: Path, sub_id: str) -> tuple[Path, str, str]:
    """Create a worktree on a new branch from the current working tree (uncommitted edits too).

    The base snapshot includes untracked, non-ignored files (``.luna/undo`` and
    ``.luna/worktrees`` excluded). ``root`` must be the git top level.
    """
    with _worktree_lock(root):
        return _create_worktree(root, sub_id)


def _prune_worktrees(root: Path) -> None:
    """Forget stale worktrees and delete unregistered leftovers under ``.luna/worktrees``."""
    _git(root, "worktree", "prune")
    holder = root / ".luna" / "worktrees"
    if not holder.is_dir():
        return
    listing = _git(root, "worktree", "list", "--porcelain").stdout
    registered = {
        Path(line[len("worktree ") :]).resolve()
        for line in listing.splitlines()
        if line.startswith("worktree ")
    }
    for entry in holder.iterdir():
        if entry.is_dir() and not entry.is_symlink() and entry.resolve() not in registered:
            shutil.rmtree(entry, ignore_errors=True)


def _create_worktree(root: Path, sub_id: str) -> tuple[Path, str, str]:
    _prune_worktrees(root)
    tree = _snapshot_tree(root)
    if tree is None:
        raise LunaCoreError("could not snapshot the working tree for the subagent")
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD").stdout.strip()
    commit = _git(
        root,
        "commit-tree",
        tree,
        "-m",
        "luna: subagent base",
        *(["-p", head] if head else []),
        env=_IDENTITY,
    )
    if commit.returncode != 0:
        raise LunaCoreError(f"git commit-tree failed: {commit.stderr.strip()}")
    base = commit.stdout.strip()
    branch = f"luna/sub/{sub_id}"
    path = root / ".luna" / "worktrees" / sub_id
    path.parent.mkdir(parents=True, exist_ok=True)
    added = _git(root, "worktree", "add", "-q", "-b", branch, str(path), base)
    if added.returncode != 0:
        raise LunaCoreError(f"git worktree add failed: {added.stderr.strip()}")
    return path, branch, base


class WorktreeCommitError(LunaCoreError):
    """The subagent's changes could not be committed; its worktree and branch are kept."""

    def __init__(self, path: Path, branch: str, detail: str) -> None:
        self.path = path
        self.branch = branch
        super().__init__(
            f"could not commit the subagent's changes ({detail.strip() or 'git failed'}); "
            f"they are kept in the worktree {path} (branch {branch})"
        )


def finish_worktree(
    root: Path, path: Path, branch: str, base: str, message: str
) -> tuple[str, str]:
    """Commit the child's changes on its branch, remove the worktree, return (diffstat, diff).

    When the child changed nothing the branch is deleted and the diffstat is empty.
    Raises :class:`WorktreeCommitError` (worktree and branch left in place) when
    the changes cannot be committed.
    """
    with _worktree_lock(root):
        return _finish_worktree(root, path, branch, base, message)


def _finish_worktree(
    root: Path, path: Path, branch: str, base: str, message: str
) -> tuple[str, str]:
    added = _git(path, "add", "-A")
    if added.returncode != 0:
        raise WorktreeCommitError(path, branch, added.stderr)
    if _git(path, "diff", "--cached", "--quiet").returncode != 0:
        # Luna's own bookkeeping commit: no user hooks, no signing prompt.
        committed = _git(
            path,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-verify",
            "-qm",
            message,
            env=_IDENTITY,
        )
        if committed.returncode != 0:
            raise WorktreeCommitError(path, branch, committed.stderr or committed.stdout)
    stat = _git(root, "diff", "--stat", base, branch).stdout.strip()
    diff = _git(root, "diff", "--binary", base, branch).stdout
    _git(root, "worktree", "remove", "--force", str(path))
    tip = _git(root, "rev-parse", "--verify", "-q", branch).stdout.strip()
    if not stat and tip == base:
        # Nothing to hand back: do not leave an empty luna/sub/* branch behind.
        _git(root, "branch", "-D", branch)
        return "", ""
    return stat, diff[:_DIFF_LIMIT]


@dataclass(frozen=True)
class SubagentResult:
    """Outcome of one delegated run."""

    name: str
    ok: bool
    text: str
    error: str = ""
    branch: str | None = None
    base: str | None = None
    diffstat: str = ""
    session_id: str = ""

    def render(self) -> str:
        """Text for the parent model."""
        head = self.text.strip() or "(the subagent gave no answer)"
        if not self.ok:
            head = f"subagent {self.name} failed: {self.error}\n\n{head}"
        if self.branch:
            head += (
                f"\n\n[changes are committed on branch {self.branch}; diff stat:\n"
                f"{self.diffstat or 'no changes'}\n"
                "apply them to the main working tree with: "
                f"git diff --binary {self.base} {self.branch} | git apply --3way --binary]"
            )
        return head


@dataclass
class SubagentRunner:
    """Runs subagents for the ``task`` tool."""

    specs: dict[str, SubagentSpec]
    max_parallel: int = 4
    model_for: Callable[[SubagentSpec], tuple[object, str]] | None = None
    _semaphore: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def describe(self) -> str:
        """``- name: description`` lines for the tool description."""
        return "\n".join(f"- {s.name}: {s.description}" for s in self.specs.values())

    def _model(self, parent, spec: SubagentSpec) -> tuple[object, str]:
        if self.model_for is not None:
            return self.model_for(spec)
        if spec.model:
            return parent.provider, spec.model
        if getattr(parent, "roles", None) is not None:
            return parent.roles.get("fast", parent.provider, parent.model)
        return parent.provider, parent.model

    async def run(
        self,
        parent,
        session: Session,
        name: str,
        task: str,
        *,
        isolation: str = "auto",
        emit: Callable[[object], None] | None = None,
        cancel: asyncio.Event | None = None,
        turn_id: str | None = None,
    ) -> SubagentResult:
        """Run subagent ``name`` on ``task`` and return its result."""
        spec = self.specs.get(name)
        if spec is None:
            return SubagentResult(
                name, False, "", f"unknown subagent; available: {', '.join(self.specs)}"
            )
        if isolation not in ISOLATION:
            return SubagentResult(name, False, "", f"isolation must be one of {ISOLATION}")
        emit = emit or (lambda event: None)
        if parent.max_cost is not None and session.cost() >= parent.max_cost:
            return self._over_budget(name, parent.max_cost)
        root = parent.workspace.root
        top = await asyncio.to_thread(_git_toplevel, root)
        mode = isolation if isolation != "auto" else spec.isolation
        if mode == "worktree" and top is None:
            return SubagentResult(name, False, "", "worktree isolation needs a git repository")
        at_top = top is not None and top == Path(root).resolve()
        if mode == "worktree" and not at_top:
            return SubagentResult(
                name,
                False,
                "",
                "worktree isolation needs the project root to be the git repository root",
            )
        use_worktree = mode == "worktree" or (mode == "auto" and spec.writes and at_top)
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_parallel)
        async with self._semaphore:
            # Siblings that ran while this one waited for a slot may have spent the budget.
            if parent.max_cost is not None and session.cost() >= parent.max_cost:
                return self._over_budget(name, parent.max_cost)
            sub_id = uuid.uuid4().hex[:8]
            message = f"luna subagent {name}: {task[:60]}"
            created: asyncio.Future | None = None
            worktree: tuple[Path, str, str] | None = None
            try:
                if use_worktree:
                    created = asyncio.ensure_future(
                        asyncio.to_thread(create_worktree, root, sub_id)
                    )
                    worktree = await asyncio.shield(created)
                    workspace = Workspace(worktree[0])
                else:
                    workspace = parent.workspace
                ok, error, text, child_id = await self._drive(
                    parent, session, spec, task, workspace, use_worktree, emit, cancel, turn_id
                )
            except BaseException:
                # Cancelled or crashed (even mid-creation): still commit what the child did
                # and drop the worktree; cleanup errors must not hide the original one.
                with contextlib.suppress(BaseException):
                    if worktree is None and created is not None:
                        worktree = await created
                    if worktree is not None:
                        await asyncio.to_thread(finish_worktree, root, *worktree, message)
                raise
            branch = base = None
            diffstat = ""
            if worktree is not None:
                try:
                    diffstat, _diff = await asyncio.to_thread(
                        finish_worktree, root, *worktree, message
                    )
                except WorktreeCommitError as exc:
                    ok, error = False, str(exc)
                if diffstat:
                    branch, base = worktree[1], worktree[2]
            return SubagentResult(name, ok, text, error, branch, base, diffstat, child_id)

    @staticmethod
    def _over_budget(name: str, max_cost: float) -> SubagentResult:
        return SubagentResult(
            name,
            False,
            "",
            f"budget reached (max_cost=${max_cost:g}); no new subagent was started",
        )

    async def _drive(
        self,
        parent,
        session: Session,
        spec: SubagentSpec,
        task: str,
        workspace: Workspace,
        use_worktree: bool,
        emit: Callable[[object], None],
        cancel: asyncio.Event | None,
        turn_id: str | None,
    ) -> tuple[bool, str, str, str]:
        """Run the child turn to the end; return ``(ok, error, answer, child_session_id)``."""
        name = spec.name
        services = dict(parent.services)
        if not use_worktree:
            services.update(undo_journal=session.undo_journal, undo_turn_id=turn_id)
        provider, model = self._model(parent, spec)
        child = Agent(
            provider,
            model,
            tools=[parent.tools[t] for t in spec.tools if t in parent.tools],
            system=build_system_prompt(spec.prompt, root=workspace.root, memory=[]),
            policy=parent.policy.child(workspace),
            workspace=workspace,
            sandbox=parent.sandbox,
            hooks=parent.hooks,
            max_steps=spec.max_steps,
            max_cost=spec.max_cost,
            locks=parent.locks,
            services=services,
            model_overrides=parent.model_overrides,
            name=name,
        )
        child_id = session.log.create(
            str(workspace.root),
            title=f"{name}: {task[:60]}",
            parent_id=session.id,
            meta={"kind": "subagent", "agent": name},
        )
        child_session = Session(session.log, child_id, workdir=str(workspace.root), undo=False)
        session.adopt(child_session)
        watcher = asyncio.create_task(self._watch(cancel, child_session)) if cancel else None
        ok, error = True, ""
        try:
            # ``aclosing`` so a cancelled parent also closes the child turn (and its tools).
            async with contextlib.aclosing(child.run(child_session, task)) as events:
                async for event in events:
                    if isinstance(event, NeedsApproval):
                        # Prefix the id: the parent and its children number calls alike.
                        request = dataclasses.replace(
                            event.request,
                            call_id=f"{child_session.id}:{event.request.call_id}",
                            description=f"{name} › {event.request.description}".rstrip(),
                        )
                        emit(NeedsApproval(request))
                    elif isinstance(event, ToolStarted):
                        emit(Notice("dim", f"{name} › {event.name} {event.preview}".rstrip()))
                    elif isinstance(event, TurnError):
                        ok, error = False, event.message
                    elif isinstance(event, Cancelled):
                        ok, error = False, "cancelled"
        finally:
            session.release(child_session)
            if watcher:
                watcher.cancel()
        answers = [m for m in child_session.messages() if m.role == "assistant" and m.text]
        return ok, error, answers[-1].text if answers else "", child_id

    @staticmethod
    async def _watch(cancel: asyncio.Event, child: Session) -> None:
        # Retry until the child's turn exists; the watcher is cancelled when the child ends.
        await cancel.wait()
        while not child.cancel_turn():
            await asyncio.sleep(0.05)


def task_tool(runner: SubagentRunner):
    """Shortcut for :func:`luna_core.tools.tasks.task_tool`."""
    from luna_core.tools.tasks import task_tool as build

    return build(runner)
