"""Split a shell command line into the simple commands that policy rules match.

Permission rules for ``execute`` are ``fnmatch`` patterns on a command string.
Matched against the whole line, ``deny = ["execute:rm *"]`` is bypassed by
``cd x && rm -rf y``. :func:`split_command` breaks a line into segments on the
shell's command separators (``&&``, ``||``, ``;``, ``|``, ``&``, newlines and
subshell parentheses), pulls the bodies of ``$(...)`` / backtick substitutions
and of ``bash -c '...'`` / ``sh -c`` / ``eval`` payloads out as segments of
their own, and normalises each segment (leading ``NAME=value`` assignments,
shell keywords and ``sudo`` / ``doas`` / ``command`` / ``env`` / ``exec`` /
``nohup`` / ``nice`` / ``stdbuf`` / ``time`` / ``timeout`` prefixes are
dropped). Quoting is respected — including bash's ``$'...'`` and here-docs:
a separator inside quotes does not split. :func:`rough_subjects` is the
quote-blind counterpart used as a safety net for deny/ask rules.

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
# Words that may precede a command without being the program itself.
_LEADING = frozenset({"{", "!", "if", "then", "else", "elif", "while", "until", "do"})
# Prefixes that run the rest of the line as a command, with the options of
# each that consume the following word.
_PREFIXES: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T"}),
    "doas": frozenset({"-u", "-C"}),
    "command": frozenset(),
    "env": frozenset({"-u", "-C", "-S"}),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "nice": frozenset({"-n"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "time": frozenset({"-f", "-o"}),
    "timeout": frozenset({"-s", "-k"}),
}
# Positional words a prefix takes before the command (``timeout 5 cmd``).
_PREFIX_POSITIONAL = {"timeout": 1}
# A redirection operator at the start of a word: ``>f``, ``2>&1``, ``<<'E'`` ...
_REDIRECT = re.compile(r"^\d*(?:<<-|<<<|<<|>>|>\||<>|>&|<&|&>>|&>|[<>])(.*)$", re.S)
# A here-doc delimiter word the splitter decodes the way the shell does.
_DELIMITER = re.compile(r"""^(?:[A-Za-z0-9_.+-]+|'[^']*'|"[^"$`\\]*")+$""")
# Two quote-blind splits: with and without braces as boundaries (a brace can
# be a group, ``{ rm x; }``, or part of a word such as a here-doc delimiter).
_ROUGH_SPLITS = (
    re.compile(r"[;&|\n`]"),
    re.compile(r"\$\(|[;&|\n()`]"),
    re.compile(r"\$\(|[;&|\n(){}`]"),
)
# A redirection operator anywhere in a rough piece.
_REDIRECT_OP = re.compile(r"\d*(?:<<-|<<<|<<|>>|>\||<>|>&|<&|&>>|&>|[<>])")
# Leading redirections with a plain target (``>/dev/null 2>&1 cmd``): dropped
# before the cuts are tried, so a run of them cannot exhaust the cut budget.
_SIMPLE_LEADING_REDIRECTS = re.compile(
    r"^(?:\d*(?:>>|>\||>&|<&|&>>|&>|<>|>|<)[^\s'\"\\$`<>|&;(){}]+\s+)+"
)
_MAX_ROUGH_CUTS = 512
#: An unparseable line longer than this is not trusted to the rough net alone.
_MAX_UNPARSEABLE = 4096
_MAX_DEPTH = 8
# Prefixes that change who runs the command or how it is looked up.
_ELEVATING_PREFIXES = frozenset({"sudo", "doas", "env", "exec", "command"})
# Variables whose assignment can make an allowed program run other code.
_INJECTING_VAR = re.compile(
    r"^(?:LD_\w*|DYLD_\w*|PATH|PYTHONPATH|PYTHONSTARTUP|PYTHONHOME|NODE_OPTIONS|NODE_PATH"
    r"|BASH_ENV|ENV|IFS|PERL5OPT|PERL5LIB|PERLLIB|RUBYOPT|RUBYLIB"
    r"|GIT_SSH|GIT_SSH_COMMAND|GIT_EXEC_PATH|GIT_CONFIG\w*|GIT_ASKPASS|GIT_EXTERNAL_DIFF"
    r"|GIT_PROXY_COMMAND|GIT_TEMPLATE_DIR)="
)


class _Unparseable(Exception):
    """The line is not something the splitter can read safely."""


@dataclass
class CommandParts:
    """The pieces of one command line.

    ``commands`` are the normalised simple commands that actually run (what
    an allow rule must cover); ``subjects`` additionally holds each raw
    segment and every wrapper (``bash -c '...'``) — everything a deny or ask
    rule is checked against. ``elevated`` lists the commands whose
    normalisation stripped a privilege/exec prefix (``sudo``, ``env``, ...)
    or an assignment of a code-injecting variable (``LD_PRELOAD=...``): an
    allow rule must not cover those.
    """

    commands: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    elevated: list[str] = field(default_factory=list)

    @property
    def first_program(self) -> str | None:
        """Program name of the first simple command, if any."""
        for command in self.commands:
            words = command.split()
            if words:
                return words[0]
        return None


def split_command(command: str, *, ansi_c: bool = True) -> CommandParts | None:
    """Return the segments of ``command``, or ``None`` when it cannot be parsed.

    ``ansi_c`` reads ``$'...'`` as bash/zsh ANSI-C quoting; with ``False`` it
    is read the POSIX ``sh`` way (``$`` followed by a single-quoted string).
    Callers that must hold for every shell parse both ways.
    """
    parts = CommandParts()
    try:
        _collect(command, parts, 0, ansi_c)
    except _Unparseable:
        return None
    return parts


def rough_subjects(command: str) -> list[str]:
    """Quote-blind pieces of ``command`` for deny/ask matching (see :func:`rough_split`)."""
    return rough_split(command)[0]


def rough_split(command: str) -> tuple[list[str], bool]:
    """Quote-blind pieces of ``command`` for deny/ask matching, and whether they are complete.

    Splits on every ``;``, ``&``, ``|``, newline and backtick (and, separately,
    also on ``( )``, ``$(`` and ``{ }``) regardless of quoting, here-docs or
    comments, tries every cut of a redirection with its target, and strips
    quote characters. It over-approximates on purpose: a deny rule may then
    also hit quoted text or a here-doc body, but no command the shell would
    run can hide inside a quoting trick or a line the splitter cannot parse.

    The second value is ``False`` when the net gave up (the cut budget ran
    out, or the line is long and cannot be parsed): callers must then fail
    closed instead of trusting the pieces.
    """
    complete = True
    if len(command) > _MAX_UNPARSEABLE and split_command(command) is None:
        complete = False
    out: list[str] = []
    # also with backslash-newline continuations joined, as the shell reads them
    texts = {command, command.replace("\\\n", "")}
    pieces = [p for text in texts for split in _ROUGH_SPLITS for p in split.split(text)]
    for piece in pieces:
        cuts, whole = _redirect_cuts(piece.strip())
        complete = complete and whole
        for cut in cuts:
            bare = cut.translate({ord(c): None for c in "'\"\\"}).strip()
            words = _normalise(bare.split())
            for variant in (cut, bare, *_variants(words)):
                if variant and variant not in out:
                    out.append(variant)
    return out, complete


def _redirect_cuts(piece: str) -> tuple[list[str], bool]:
    """Return ``piece`` plus forms with a redirection and a guessed target cut out.

    Quote-blind, the target's extent is unknown (``>'a b' rm``, an escaped
    space in a here-doc delimiter), so every whitespace after the operator
    is tried. The flag is ``False`` when the cut budget ran out.
    """
    cuts = [piece] if piece else []
    piece = _SIMPLE_LEADING_REDIRECTS.sub("", piece)
    if piece and piece not in cuts:
        cuts.append(piece)
    for op in _REDIRECT_OP.finditer(piece):
        for gap in re.finditer(r"\s+", piece[op.end() :]):
            if len(cuts) >= _MAX_ROUGH_CUTS:
                return cuts, False
            cut = (piece[: op.start()] + piece[op.end() + gap.end() :]).strip()
            if cut and cut not in cuts:
                cuts.append(cut)
    return cuts, True


def _variants(words: list[str]) -> list[str]:
    """Return the joined command, plus a form with the program path cut to its basename."""
    if not words:
        return []
    out = [" ".join(words)]
    if "/" in words[0]:
        out.append(" ".join([words[0].rsplit("/", 1)[-1], *words[1:]]))
    return out


def _collect(
    text: str, parts: CommandParts, depth: int, ansi_c: bool, elevated: bool = False
) -> None:
    if depth > _MAX_DEPTH:
        raise _Unparseable("nested too deeply")
    segments, substitutions = _scan(text, ansi_c)
    for raw in segments:
        raw = raw.strip()
        if not raw:
            continue
        try:
            words = shlex.split(raw, comments=False, posix=True)
        except ValueError as exc:
            raise _Unparseable(str(exc)) from exc
        flags: list[str] = []
        words = _normalise(words, flags)
        segment_elevated = elevated or bool(flags)
        if raw not in parts.subjects:
            parts.subjects.append(raw)
        if not words:
            continue
        payload = _payload(words)
        normalised = " ".join(words)
        for variant in _variants(words):
            if variant not in parts.subjects:
                parts.subjects.append(variant)
        if payload is None:
            parts.commands.append(normalised)
            if segment_elevated:
                parts.elevated.append(normalised)
        else:
            _collect(payload, parts, depth + 1, ansi_c, segment_elevated)
    for body in substitutions:
        _collect(body, parts, depth + 1, ansi_c)


def _normalise(words: list[str], flags: list[str] | None = None) -> list[str]:
    """Drop leading assignments, keywords, group braces and command-runner prefixes.

    Stripped privilege/exec prefixes and code-injecting assignments are
    appended to ``flags`` when given.
    """
    words = list(words)
    flags = [] if flags is None else flags
    changed = True
    while words and changed:
        changed = False
        while words and (_ASSIGNMENT.match(words[0]) or words[0] in _LEADING):
            if _INJECTING_VAR.match(words[0]):
                flags.append(words[0])
            words.pop(0)
            changed = True
        if words and words[-1] == "}":
            words.pop()
            changed = True
        while words and (redirect := _REDIRECT.match(words[0])):
            words.pop(0)  # a leading redirection and its target
            if not redirect.group(1) and words:
                words.pop(0)
            changed = True
        if words and words[0] in _PREFIXES:
            prefix = words.pop(0)
            if prefix in _ELEVATING_PREFIXES:
                flags.append(prefix)
            takes_value = _PREFIXES[prefix]
            while words and words[0].startswith("-"):
                option = words.pop(0)
                if option == "--":
                    break
                if option in takes_value and words:
                    words.pop(0)
            del words[: _PREFIX_POSITIONAL.get(prefix, 0)]
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


_ANSI_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b"}


def _ansi_c_end(text: str, start: int) -> tuple[int, str]:
    """Read a ``$'...'`` body from ``start``; return the closing index and its value."""
    out: list[str] = []
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append(_ANSI_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if ch == "'":
            return i, "".join(out)
        out.append(ch)
        i += 1
    raise _Unparseable("unbalanced $'...' quoting")


def _here_doc(text: str, i: int) -> tuple[int, tuple[str, bool, bool]]:
    """Parse ``<<[-]WORD`` at ``text[i:]``; return the end index and (delim, strip, quoted)."""
    j = i + 2
    strip = text.startswith("-", j)
    j += strip
    while j < len(text) and text[j] in " \t":
        j += 1
    start = j
    while j < len(text) and not text[j].isspace() and text[j] not in ";&|<>()":
        j += 1
    word = text[start:j]
    if not _DELIMITER.match(word):  # ``E$""``, ``E\``, ...: fail closed
        raise _Unparseable(f"here-doc delimiter {word!r} is not plain")
    quoted = any(c in word for c in "'\"")
    delim = word.translate({ord(c): None for c in "'\""})
    if not delim:
        raise _Unparseable("here-doc without a delimiter")
    return j, (delim, strip, quoted)


def _here_doc_bodies(
    text: str, i: int, pending: list[tuple[str, bool, bool]]
) -> tuple[int, list[str]]:
    """Skip the bodies of ``pending`` here-docs starting at ``i``.

    Returns the index after the last body and the bodies of unquoted ones
    (they still undergo ``$(...)`` / backtick substitution).
    """
    expanded: list[str] = []
    for delim, strip, quoted in pending:
        lines: list[str] = []
        while True:
            if i >= len(text):  # the shell would read a different line as the end
                raise _Unparseable(f"here-doc {delim!r} is never terminated")
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            line = text[i:end]
            i = end + 1
            if (line.lstrip("\t") if strip else line) == delim:
                break
            lines.append(line)
        if not quoted:
            expanded.append("\n".join(lines))
    return min(i, len(text)), expanded


def _substitutions_in(body: str, ansi_c: bool) -> list[str]:
    """Return the bodies of the ``$(...)`` and backtick substitutions in here-doc text."""
    found: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
        elif body.startswith("$(", i):
            end = _closing_paren(body, i + 2, ansi_c)
            found.append(body[i + 2 : end])
            i = end + 1
        elif body[i] == "`":
            end = _closing_backtick(body, i + 1)
            found.append(body[i + 1 : end])
            i = end + 1
        else:
            i += 1
    return found


def _scan(text: str, ansi_c: bool = True) -> tuple[list[str], list[str]]:
    """Split ``text`` on unquoted separators; collect substitution bodies.

    Returns the raw segments (substitutions left in place, ``$'...'`` turned
    into an equivalent single-quoted word, here-doc bodies removed) and the
    bodies of every ``$(...)``, ``<(...)``, ``>(...)`` and backtick
    substitution, including those inside unquoted here-docs.
    """
    segments: list[str] = []
    substitutions: list[str] = []
    current: list[str] = []
    pending: list[tuple[str, bool, bool]] = []
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
            if text.startswith("\n", i + 1):  # line continuation
                i += 2
                continue
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
            end = _closing_paren(text, i + 1, ansi_c)
            substitutions.append(text[i + 1 : end])
            current.append(text[i : end + 1])
            i = end + 1
            continue
        if text.startswith("${", i):
            end = _closing_brace(text, i + 2, ansi_c)
            substitutions += _substitutions_in(text[i + 2 : end], ansi_c)
            current.append(text[i : end + 1])
            i = end + 1
            continue
        if quote == '"':
            current.append(ch)
            if ch == '"':
                quote = None
            i += 1
            continue
        if ansi_c and text.startswith("$'", i):
            end, value = _ansi_c_end(text, i + 2)
            current.append(shlex.quote(value))
            i = end + 1
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
        if text.startswith("<<", i) and not text.startswith("<<<", i):
            end, spec = _here_doc(text, i)
            pending.append(spec)
            current.append(text[i:end])
            i = end
            continue
        if text.startswith(("&&", "||"), i):
            cut()
            i += 2
            continue
        if ch in "&|" and (
            (current and current[-1][-1:] in "<>") or text.startswith("&>", i)
        ):  # ``2>&1`` / ``&>file`` / ``>|file`` are redirections, not separators
            current.append(ch)
            i += 1
            continue
        if ch == "\n" and pending:
            cut()
            i, bodies = _here_doc_bodies(text, i + 1, pending)
            for body in bodies:
                substitutions += _substitutions_in(body, ansi_c)
            pending.clear()
            continue
        if ch in ";|&\n()":
            cut()
            i += 1
            continue
        current.append(ch)
        i += 1
    if quote is not None:
        raise _Unparseable("unbalanced quotes")
    if pending:
        raise _Unparseable("here-doc without a body")
    cut()
    return segments, substitutions


def _closing_brace(text: str, start: int, ansi_c: bool) -> int:
    """Return the index of the ``}`` closing a ``${`` that ends just before ``start``."""
    depth, i, quote = 1, start, None
    while i < len(text):
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
        elif ch == "\\":
            i += 2
            continue
        elif text.startswith("$(", i):
            i = _closing_paren(text, i + 2, ansi_c) + 1
            continue
        elif ch == "`":
            i = _closing_backtick(text, i + 1) + 1
            continue
        elif text.startswith("${", i):
            depth += 1
            i += 2
            continue
        elif quote == '"':
            if ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise _Unparseable("unbalanced ${...}")


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


def _closing_paren(text: str, start: int, ansi_c: bool = True) -> int:
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
        elif ansi_c and text.startswith("$'", i):
            i = _ansi_c_end(text, i + 2)[0] + 1
            continue
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
