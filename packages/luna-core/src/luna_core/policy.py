"""Permission rules, approvals, plan mode and stale-edit anchors."""

from __future__ import annotations

import hashlib
import posixpath
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from luna_core.events import ApprovalRequest
from luna_core.tools.base import Tool, args_preview
from luna_core.workspace import OutsideWorkspace, Workspace

MUTATING_FILE_TOOLS = frozenset({"write_file", "edit_file", "delete"})
DEFAULT_APPROVAL = frozenset({"write_file", "edit_file", "delete", "execute"})
_GROUPS = {
    "write": MUTATING_FILE_TOOLS,
    "fs": frozenset({"write_file", "edit_file", "delete", "read_file", "ls", "glob", "grep"}),
}


def _relpath(raw: str) -> str:
    return PurePosixPath(str(raw)).as_posix().lstrip("/") if raw else ""


def _subject(tool: str, args: dict) -> str:
    if tool == "execute":
        return str(args.get("command", ""))
    return _relpath(args.get("file_path", args.get("path", "")))


def _tool_matches(rule_tool: str, tool: str) -> bool:
    return rule_tool in ("*", tool) or tool in _GROUPS.get(rule_tool, ())


@dataclass
class Rules:
    """Allow / ask / deny rules of the form ``<tool or group>:<fnmatch pattern>``."""

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)

    def _hit(self, rules: list[str], tool: str, subject: str) -> bool:
        for rule in rules:
            rtool, _, pattern = rule.partition(":")
            if not _tool_matches(rtool, tool):
                continue
            if tool != "execute":
                pattern = _relpath(pattern)
            if fnmatch(subject, pattern):
                return True
        return False

    def match(self, tool: str, args: dict) -> str | None:
        """``"deny"``, ``"ask"``, ``"allow"`` or ``None`` (deny wins)."""
        subject = _subject(tool, args)
        for kind in ("deny", "ask", "allow"):
            if self._hit(getattr(self, kind), tool, subject):
                return kind
        return None

    @classmethod
    def load(cls, paths: list[Path]) -> Rules:
        """Merge rule files in order (missing or broken files are skipped)."""
        rules = cls()
        for path in paths:
            data = _read(Path(path))
            table = data.get("permissions", data)
            if not isinstance(table, dict):
                continue
            for kind in ("allow", "deny", "ask"):
                values = table.get(kind, [])
                if isinstance(values, list):
                    dest = getattr(rules, kind)
                    dest += [r for r in values if isinstance(r, str) and r not in dest]
        return rules


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def append_rule(path: Path, rule: str, kind: str = "allow") -> Path:
    """Add ``rule`` to ``kind`` in a ``[permissions]`` TOML file, keeping the rest."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read(path)
    table = data.get("permissions", data) if data else {}
    lists = {k: list(table.get(k, [])) for k in ("allow", "deny", "ask")}
    if rule not in lists[kind]:
        lists[kind].append(rule)
    lines = ["[permissions]"]
    for k, values in lists.items():
        if values:
            lines += [f"{k} = [", *(f"    {_toml_str(r)}," for r in values), "]"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def suggest_rule(tool: str, args: dict) -> str:
    """Build a rule that would allow this call next time."""
    if tool == "execute":
        first = str(args.get("command", "")).split()
        return f"execute:{first[0]} *" if first else "execute:*"
    return f"{tool}:{_subject(tool, args)}"


@dataclass(frozen=True)
class Decision:
    """A human answer to an approval request."""

    type: str
    args: dict | None = None
    message: str = ""
    always: str | None = None

    @classmethod
    def approve(cls, always: str | None = None) -> Decision:
        """Run the call as proposed."""
        return cls("approve", always=always)

    @classmethod
    def edit(cls, args: dict) -> Decision:
        """Run the call with edited arguments."""
        return cls("edit", args=dict(args))

    @classmethod
    def reject(cls, message: str = "") -> Decision:
        """Do not run the call; the model sees ``message``."""
        return cls("reject", message=message)

    @classmethod
    def from_dict(cls, data: dict) -> Decision:
        """Parse the wire form (``{"type": ..., "args"|"message"|"always": ...}``)."""
        kind = data.get("type")
        if kind not in ("approve", "edit", "reject"):
            raise ValueError(f"unknown decision type {kind!r}")
        return cls(kind, data.get("args"), data.get("message", ""), data.get("always"))

    def to_dict(self) -> dict:
        """Wire form."""
        out: dict = {"type": self.type}
        if self.args is not None:
            out["args"] = self.args
        if self.message:
            out["message"] = self.message
        if self.always:
            out["always"] = self.always
        return out


@dataclass(frozen=True)
class Verdict:
    """What the policy says about one tool call."""

    action: str
    message: str = ""
    request: ApprovalRequest | None = None


class AnchorTracker:
    """Last-seen content hash per path; edits on changed files are refused."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._anchors: dict[str, str] = {}

    @staticmethod
    def _key(rel: str) -> str:
        return posixpath.normpath(rel.lstrip("/"))

    def _hash(self, key: str) -> str | None:
        try:
            return hashlib.sha256((self.root / key).read_bytes()).hexdigest()
        except OSError:
            return None

    def remember(self, rel: str) -> None:
        """Record the file's current content as seen."""
        key = self._key(rel)
        digest = self._hash(key)
        if digest is not None:
            self._anchors[key] = digest

    def check(self, rel: str) -> bool:
        """Return True when there is no anchor or the file still matches it."""
        key = self._key(rel)
        anchored = self._anchors.get(key)
        return anchored is None or self._hash(key) == anchored

    def forget(self, rel: str) -> None:
        """Drop one anchor."""
        self._anchors.pop(self._key(rel), None)

    def forget_under(self, rel: str) -> None:
        """Drop an anchor and every anchor below it."""
        prefix = self._key(rel)
        self._anchors.pop(prefix, None)
        for key in [k for k in self._anchors if k.startswith(prefix + "/")]:
            del self._anchors[key]


class Policy:
    """Decides, per tool call, whether to run it, refuse it or ask a human."""

    def __init__(
        self,
        rules: Rules | None = None,
        *,
        approval_tools: frozenset[str] = DEFAULT_APPROVAL,
        yolo: bool = False,
        plan_mode: bool | Callable[[], bool] = False,
        workspace: Workspace | None = None,
        persist_rule: Callable[[str], None] | None = None,
        anchors: AnchorTracker | None = None,
    ) -> None:
        self.rules = rules or Rules()
        self.approval_tools = frozenset(approval_tools)
        self.yolo = yolo
        self.plan_mode = plan_mode
        self.workspace = workspace
        self.persist_rule = persist_rule
        self.anchors = anchors or (AnchorTracker(workspace.root) if workspace else None)

    def _planning(self) -> bool:
        return bool(self.plan_mode()) if callable(self.plan_mode) else bool(self.plan_mode)

    @property
    def planning(self) -> bool:
        """True while plan mode refuses mutating tool calls."""
        return self._planning()

    def _rel(self, args: dict) -> str:
        raw = args.get("file_path") or args.get("path") or ""
        if not raw or self.workspace is None:
            return _relpath(raw)
        try:
            return self.workspace.rel(self.workspace.resolve(raw))
        except OutsideWorkspace:
            return _relpath(raw)

    def check(self, tool: Tool, args: dict) -> Verdict:
        """Verdict for calling ``tool`` with ``args``."""
        name = tool.name
        match = self.rules.match(name, args)
        if match == "deny":
            return Verdict("deny", f"blocked by a Luna permission rule ({name})")
        mutating = tool.mutating or name in self.approval_tools
        if mutating and self._planning():
            return Verdict(
                "deny", f"plan mode is on — refusing to {name}. Run /plan off to make changes."
            )
        rel = self._rel(args)
        if rel and name in MUTATING_FILE_TOOLS and self.anchors and not self.anchors.check(rel):
            return Verdict(
                "deny",
                f"{rel} has changed on disk since Luna last saw it (hand edit, another "
                "process, or a formatter) — read it again before retrying",
            )
        if match == "allow" or (self.yolo and match != "ask"):
            return Verdict("allow")
        if match == "ask" or name in self.approval_tools:
            request = ApprovalRequest(
                "", name, dict(args), args_preview(args, 80), suggest_rule(name, args)
            )
            return Verdict("ask", request=request)
        return Verdict("allow")

    def after(self, name: str, args: dict, ok: bool) -> None:
        """Update anchors after a tool ran."""
        if self.anchors is None:
            return
        rel = self._rel(args)
        if not rel or name not in {"read_file", *MUTATING_FILE_TOOLS}:
            return
        if name == "delete":
            if ok:
                self.anchors.forget_under(rel)
        elif name == "read_file" and not ok:
            self.anchors.forget(rel)
        elif ok:
            self.anchors.remember(rel)

    def remember_always(self, rule: str) -> None:
        """Allow ``rule`` for the rest of the session (and persist it if configured)."""
        if rule not in self.rules.allow:
            self.rules.allow.append(rule)
        if self.persist_rule:
            self.persist_rule(rule)

    def child(self, workspace: Workspace | None = None) -> Policy:
        """Build a policy for a subagent: same rules and plan mode.

        Anchors are shared when the child works in the same workspace and
        fresh when it works elsewhere (a git worktree).
        """
        same = workspace is None or (
            self.workspace is not None and workspace.root == self.workspace.root
        )
        return Policy(
            self.rules,
            approval_tools=self.approval_tools,
            yolo=self.yolo,
            plan_mode=self.plan_mode,
            workspace=self.workspace if workspace is None else workspace,
            persist_rule=self.persist_rule,
            anchors=self.anchors if same else None,
        )
