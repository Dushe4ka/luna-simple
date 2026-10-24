"""Split a shell command line into the simple commands that policy rules match.

Permission rules for ``execute`` are ``fnmatch`` patterns on a command string.
Matched against the whole line, ``deny = ["execute:rm *"]`` is bypassed by
``cd x && rm -rf y``. :func:`split_command` breaks a line into segments on the
shell's command separators (``&&``, ``||``, ``;``, ``|``, ``&``, newlines and
subshell parentheses), pulls the bodies of ``$(...)`` / backtick substitutions
and of ``bash -c '...'`` / ``sh -c`` / ``eval`` payloads out as segments of
their own, and normalises each segment (leading ``NAME=value`` assignments and
``sudo`` / ``command`` / ``env`` / ``exec`` / ``nohup`` / ``time`` prefixes are
dropped). Quoting is respected: a separator inside quotes does not split.

This is a conservative approximation of the shell grammar for policy purposes,
not a full parser: a line it cannot read (unbalanced quotes or parentheses)
yields ``None``, and callers must then refuse to auto-allow it.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# Prefixes that run the rest of the line as a command, with the options of
# each that consume the following word.
_PREFIXES: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T"}),
    "command": frozenset(),
    "env": frozenset({"-u", "-C", "-S"}),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "time": frozenset({"-f", "-o"}),
}
_MAX_DEPTH = 8


class _Unparseable(Exception):
    """The line is not something the splitter can read safely."""


@dataclass
class CommandParts:
    """The pieces of one command line.

    ``commands`` are the normalised simple commands that actually run (what
    an allow rule must cover); ``subjects`` additionally holds each raw
    segment and every wrapper (``bash -c '...'``) — everything a deny or ask
    rule is checked against.
    """

    commands: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)

    @property
    def first_program(self) -> str | None:
        """Program name of the first simple command, if any."""
        for command in self.commands:
            words = command.split()
            if words:
                return words[0]
        return None


def split_command(command: str) -> CommandParts | None:
    """Return the segments of ``command``, or ``None`` when it cannot be parsed."""
    parts = CommandParts()
    try:
        _collect(command, parts, 0)
    except _Unparseable:
        return None
    return parts


def _collect(text: str, parts: CommandParts, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise _Unparseable("nested too deeply")
    segments, substitutions = _scan(text)
    for raw in segments:
        raw = raw.strip()
        if not raw:
            continue
        try:
            words = shlex.split(raw, comments=False, posix=True)
        except ValueError as exc:
            raise _Unparseable(str(exc)) from exc
        words = _normalise(words)
        if raw not in parts.subjects:
            parts.subjects.append(raw)
        if not words:
            continue
        payload = _payload(words)
        normalised = " ".join(words)
        if normalised not in parts.subjects:
            parts.subjects.append(normalised)
        if payload is None:
            parts.commands.append(normalised)
        else:
            _collect(payload, parts, depth + 1)
    for body in substitutions:
        _collect(body, parts, depth + 1)


def _normalise(words: list[str]) -> list[str]:
    """Drop leading assignments, group braces and command-runner prefixes."""
    words = list(words)
    changed = True
    while words and changed:
        changed = False
        while words and (_ASSIGNMENT.match(words[0]) or words[0] in ("{", "!")):
            words.pop(0)
            changed = True
        if words and words[-1] == "}":
            words.pop()
            changed = True
        if words and words[0] in _PREFIXES:
            takes_value = _PREFIXES[words.pop(0)]
            while words and words[0].startswith("-"):
                option = words.pop(0)
                if option == "--":
                    break
                if option in takes_value and words:
                    words.pop(0)
            changed = True
    return words


def _payload(words: list[str]) -> str | None:
    """Return the command string run by ``bash -c '...'`` / ``eval ...``, else ``None``."""
    program = words[0].rsplit("/", 1)[-1]
    if program == "eval":
        return " ".join(words[1:])
    if program not in _SHELLS:
        return None
    for index, word in enumerate(words[1:], 1):
        if not word.startswith("-") or word == "--":
            return None
        if not word.startswith("--") and "c" in word[1:]:
            return words[index + 1] if index + 1 < len(words) else ""
    return None


def _scan(text: str) -> tuple[list[str], list[str]]:
    """Split ``text`` on unquoted separators; collect substitution bodies.

    Returns the raw segments (substitutions left in place) and the bodies of
    every ``$(...)``, ``<(...)``, ``>(...)`` and backtick substitution.
    """
    segments: list[str] = []
    substitutions: list[str] = []
    current: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None

    def cut() -> None:
        segments.append("".join(current))
        current.clear()

    while i < n:
        ch = text[i]
        if quote == "'":
            current.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\":
            current.append(text[i : i + 2])
            i += 2
            continue
        if ch == "`":
            end = _closing_backtick(text, i + 1)
            substitutions.append(text[i + 1 : end])
            current.append(text[i : end + 1])
            i = end + 1
            continue
        if ch == "(" and i > 0 and text[i - 1] in "$<>":
            end = _closing_paren(text, i + 1)
            substitutions.append(text[i + 1 : end])
            current.append(text[i : end + 1])
            i = end + 1
            continue
        if quote == '"':
            current.append(ch)
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch == "#" and (not current or current[-1][-1:].isspace()):
            while i < n and text[i] != "\n":  # comment up to the end of the line
                i += 1
            continue
        if text.startswith(("&&", "||"), i):
            cut()
            i += 2
            continue
        if ch == "&" and (
            (current and current[-1][-1:] in "<>") or text.startswith("&>", i)
        ):  # ``2>&1`` / ``&>file`` are redirections, not separators
            current.append(ch)
            i += 1
            continue
        if ch in ";|&\n()":
            cut()
            i += 1
            continue
        current.append(ch)
        i += 1
    if quote is not None:
        raise _Unparseable("unbalanced quotes")
    cut()
    return segments, substitutions


def _closing_backtick(text: str, start: int) -> int:
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`":
            return i
        i += 1
    raise _Unparseable("unbalanced backticks")


def _closing_paren(text: str, start: int) -> int:
    """Return the index of the ``)`` closing a ``(`` opened just before ``start``."""
    depth, i, quote = 1, start, None
    while i < len(text):
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
        elif ch == "\\":
            i += 2
            continue
        elif quote == '"':
            if ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise _Unparseable("unbalanced parentheses")
