"""Permission rules, approvals, plan mode and stale-edit anchors."""

from __future__ import annotations

import hashlib
import posixpath
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from luna_core.errors import LunaCoreError, ToolError
from luna_core.events import ApprovalRequest
from luna_core.shellsplit import rough_subjects, split_command
from luna_core.tools.base import Tool, args_preview
from luna_core.workspace import OutsideWorkspace, Workspace

MUTATING_FILE_TOOLS = frozenset({"write_file", "edit_file", "delete"})
DEFAULT_APPROVAL = frozenset({"write_file", "edit_file", "delete", "execute"})
_GROUPS = {
    "write": MUTATING_FILE_TOOLS,
    "fs": frozenset({"write_file", "edit_file", "delete", "read_file", "ls", "glob", "grep"}),
}


def _relpath(raw: str) -> str:
    """Project-relative POSIX form of ``raw``, with ``..``/``.``/``/`` collapsed.

    ``PurePosixPath.as_posix()`` alone does not collapse ``..`` segments (so
    ``src/../.env`` would stay distinct from ``.env``), which let a deny rule
    on ``.env`` be bypassed by a non-normalised path. ``posixpath.normpath``
    closes that gap for the no-workspace case; ``Policy.check`` additionally
    resolves file-tool paths through the real :class:`Workspace` first.
    """
    if not raw:
        return ""
    posix = PurePosixPath(str(raw)).as_posix()
    return posixpath.normpath(posix).lstrip("/")


def _subject(tool: str, args: dict) -> str:
    if tool == "execute":
        return str(args.get("command", "")).strip()
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
            # ``execute:ls *`` also covers a bare ``ls``
            if tool == "execute" and pattern.endswith(" *") and fnmatch(subject, pattern[:-2]):
                return True
        return False

    def match(self, tool: str, args: dict) -> str | None:
        """``"deny"``, ``"ask"``, ``"allow"`` or ``None`` (deny wins)."""
        subject = _subject(tool, args)
        if tool == "execute":
            return self._match_execute(subject)
        for kind in ("deny", "ask", "allow"):
            if self._hit(getattr(self, kind), tool, subject):
                return kind
        return None

    def _match_execute(self, command: str) -> str | None:
        """Match a shell line segment by segment (see :mod:`luna_core.shellsplit`).

        deny / ask: the whole line, any parsed segment or any quote-blind
        rough piece matching is enough. allow: every simple command must
        match under both the bash (``$'...'`` as ANSI-C quoting) and the POSIX
        ``sh`` reading; a line that cannot be parsed is never allowed by a rule.
        """
        readings = [split_command(command)]
        if "$'" in command:
            readings.append(split_command(command, ansi_c=False))
        subjects = [command, *rough_subjects(command)]
        for parts in readings:
            subjects += parts.subjects if parts else ()
        for kind in ("deny", "ask"):
            rules = getattr(self, kind)
            if any(self._hit(rules, "execute", s) for s in subjects):
                return kind
        if any(parts is None for parts in readings):
            return None
        commands = [c for parts in readings for c in parts.commands] or [command]
        if all(self._hit(self.allow, "execute", c) for c in commands):
            return "allow"
        return None

    @classmethod
    def load(cls, paths: list[Path]) -> Rules:
        """Merge rule files in order; a missing file is skipped.

        Fails closed: an unreadable or invalid file raises
        :class:`RulesFileError` instead of being skipped, because skipping
        it would silently drop its deny rules.
        """
        rules = cls()
        for path in paths:
            data = _read(Path(path))
            table = data.get("permissions", data)
            if not isinstance(table, dict):
                raise RulesFileError(f"{path}: [permissions] must be a table")
            for kind in ("allow", "deny", "ask"):
                values = table.get(kind, [])
                if isinstance(values, list):
                    dest = getattr(rules, kind)
                    dest += [r for r in values if isinstance(r, str) and r not in dest]
        return rules


class RulesFileError(LunaCoreError):
    """A permission rules file exists but cannot be read or parsed."""


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RulesFileError(
            f"cannot load permission rules from {path}: {exc} "
            "(fix or remove the file; its deny rules would otherwise be lost)"
        ) from exc


_PERM_HEADER = re.compile(r"^\[\s*permissions\s*\][ \t]*(#.*)?$", re.MULTILINE)
_NEXT_HEADER = re.compile(r"^\[", re.MULTILINE)


def append_rule(path: Path, rule: str, kind: str = "allow") -> Path:
    """Add ``rule`` to ``kind`` in a ``[permissions]`` TOML file.

    A missing file is created with a bare ``[permissions]`` table. An
    existing file must already parse as TOML — a broken file is left
    untouched and raises :class:`ValueError` rather than being silently
    overwritten (which would otherwise drop every rule already in it).

    When the file holds nothing but the permissions table (or the project's
    bare ``permissions.toml`` form, with ``allow``/``deny``/``ask`` at the
    top level and no other keys), the whole file is rewritten, same as
    before. When it also holds unrelated tables (e.g. a shared
    ``config.toml`` with a ``[model]`` section), only the ``[permissions]``
    section is replaced — every other line is kept byte-for-byte.
    """
    path = Path(path)
    raw = ""
    data: dict = {}
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        try:
            data = tomllib.loads(raw) if raw.strip() else {}
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path} is not valid TOML: {exc}") from exc
    has_header = "permissions" in data
    table = data["permissions"] if has_header else data
    foreign = set(data) - ({"permissions"} if has_header else {"allow", "deny", "ask"})
    lists = {k: list(table.get(k, [])) for k in ("allow", "deny", "ask")}
    if rule not in lists[kind]:
        lists[kind].append(rule)
    block = _permissions_block(lists)
    text = block if not foreign else _splice_permissions(raw, block)
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"cannot add a rule to {path} without breaking it ({exc}); edit it by hand"
        ) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _permissions_block(lists: dict[str, list[str]]) -> str:
    lines = ["[permissions]"]
    for k in ("allow", "deny", "ask"):
        values = lists[k]
        if values:
            lines += [f"{k} = [", *(f"    {_toml_str(r)}," for r in values), "]"]
    return "\n".join(lines) + "\n"


def _splice_permissions(raw: str, block: str) -> str:
    """Replace the ``[permissions]`` table in ``raw`` with ``block``, or append it."""
    match = _PERM_HEADER.search(raw)
    if not match:
        sep = "" if not raw else ("\n" if raw.endswith("\n") else "\n\n")
        return raw + sep + block
    rest = raw[match.end() :]
    nxt = _NEXT_HEADER.search(rest)
    end = match.end() + (nxt.start() if nxt else len(rest))
    return raw[: match.start()] + block + raw[end:]


_CONTROL_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}


def _toml_str(value: str) -> str:
    """Build a TOML basic string literal, escaping backslashes, quotes and control characters."""
    out = []
    for ch in value:
        if ch in _CONTROL_ESCAPES:
            out.append(_CONTROL_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _program_rule(command: str) -> str:
    words = command.split()
    return f"execute:{words[0]} *" if words else "execute:*"


def suggest_rules(tool: str, args: dict, rules: Rules | None = None) -> tuple[str, ...]:
    """Build every rule that would allow this call next time.

    For ``execute`` that is one ``execute:<program> *`` rule per simple
    command of the line (see :mod:`luna_core.shellsplit`), skipping commands
    ``rules`` already allow. Falls back to :func:`suggest_rule` alone.
    """
    if tool != "execute":
        return (suggest_rule(tool, args),)
    command = str(args.get("command", ""))
    out: list[str] = []
    for reading in (split_command(command), split_command(command, ansi_c=False)):
        for simple in reading.commands if reading else ():
            if rules is not None and rules._hit(rules.allow, "execute", simple):
                continue
            rule = _program_rule(simple)
            if rule not in out:
                out.append(rule)
    return tuple(out) or (suggest_rule(tool, args),)


def suggest_rule(tool: str, args: dict) -> str:
    """Build a rule that would allow this call next time."""
    if tool == "execute":
        command = str(args.get("command", ""))
        parts = split_command(command)
        program = parts.first_program if parts else None
        if program is None:
            words = command.split()
            program = words[0] if words else None
        return f"execute:{program} *" if program else "execute:*"
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

    def _rel(self, args: dict) -> str | None:
        """Workspace-relative path of the call's file argument; ``None`` when it is invalid."""
        raw = args.get("file_path") or args.get("path") or ""
        if not raw or self.workspace is None:
            return _relpath(raw)
        try:
            return self.workspace.rel(self.workspace.resolve(raw))
        except OutsideWorkspace:
            return _relpath(raw)
        except (ToolError, OSError, ValueError):  # InvalidPath, NUL byte, OS refusal
            return None

    def check(self, tool: Tool, args: dict) -> Verdict:
        """Verdict for calling ``tool`` with ``args``."""
        name = tool.name
        rel = self._rel(args)
        if rel is None:  # fail closed: a path that cannot exist is never worth asking about
            return Verdict("deny", f"invalid path: {args.get('file_path') or args.get('path')!r}")
        # Rule subjects use the workspace-resolved relative path, not the raw
        # argument, so a deny rule cannot be bypassed by an unnormalised path
        # (e.g. ``src/../.env`` must match the same rule as ``.env``).
        match_args = {**args, "file_path": rel} if rel and name != "execute" else args
        match = self.rules.match(name, match_args)
        if match == "deny":
            return Verdict("deny", f"blocked by a Luna permission rule ({name})")
        mutating = tool.mutating or name in self.approval_tools
        if mutating and self.planning:
            return Verdict(
                "deny", f"plan mode is on — refusing to {name}. Run /plan off to make changes."
            )
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
                "",
                name,
                dict(args),
                args_preview(args, 80),
                suggest_rule(name, args),
                suggest_rules(name, args, self.rules),
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
