"""Distill the assembly journal into reusable profiles.

One request to a cheap model groups similar assemblies and proposes a
generalised profile per group; code — not the model — then decides what is
kept: enough runs, enough successes, tools and skills the runs really used,
no hand-written name taken, writing rights within ``allow_write``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from luna_core.assemblies import RunRecord
from luna_core.errors import LunaCoreError, ProviderError
from luna_core.messages import Message
from luna_core.providers.base import ModelRequest, Provider, StreamAccumulator
from luna_core.subagents import CHILD_FORBIDDEN, WRITE_TOOLS, SubagentSpec

MIN_RUNS = 5
MIN_SUCCESS = 0.7
DISABLE_WINDOW = 10
DISABLE_MIN_RUNS = 5
DISABLE_BELOW = 0.5
MAX_OUTPUT_TOKENS = 4000
FIELD_CHARS = 1500
INPUT_CHARS = 60_000
MAX_STEPS_CAP = 200

_NAME = re.compile(r"^[a-z][a-z0-9-]{1,39}$")

DISTILL_PROMPT = """\
You review a journal of subagent assemblies. Each line is one run: the prompt,
tools, skills, model and context the parent agent gave a subagent, the first
words of its task, and whether the run succeeded.

Find groups of runs that do the same kind of job, and propose one reusable
profile per group. Rules:
- Group only runs that really do the same kind of job.
- Generalise the prompt: no file names, ids or details of a single task.
- Use only tools and skills that the grouped runs used.
- List the ids of the runs in each group under "runs".
- Name profiles in kebab-case (a-z, 0-9, dashes), at most 40 characters.

Reply with JSON only, in this shape:
{"profiles": [{"name": "...", "description": "...", "prompt": "...",
  "tools": ["..."], "skills": ["..."], "model": null, "context": "",
  "max_steps": 30, "runs": [1, 2, 3]}]}
Reply {"profiles": []} when nothing repeats.
"""


class DistillError(LunaCoreError):
    """The analysis request failed."""


@dataclass(frozen=True)
class Candidate:
    """A profile the model proposed for a group of runs."""

    name: str
    description: str
    prompt: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    model: str | None
    context: str
    max_steps: int
    runs: tuple[int, ...]

    def to_spec(self, *, unsafe: bool, version: int) -> SubagentSpec:
        """Return the profile to save (always ``auto``)."""
        return SubagentSpec(
            self.name,
            self.description,
            self.prompt,
            self.tools,
            self.model,
            unsafe,
            "auto",
            self.max_steps,
            None,
            skills=self.skills,
            context=self.context,
            auto=True,
            version=version,
        )


@dataclass(frozen=True)
class Verdict:
    """What the code decided about one candidate."""

    candidate: Candidate
    ok: bool
    reason: str
    runs: tuple[int, ...]
    successes: int
    workdirs: tuple[str, ...]
    writes: bool


def _cut(text: str) -> str:
    return text if len(text) <= FIELD_CHARS else text[:FIELD_CHARS] + "…"


def render_journal(records: Sequence[RunRecord]) -> str:
    """One JSON line per run, newest kept when the whole exceeds ``INPUT_CHARS``."""
    lines: list[str] = []
    total = 0
    for r in reversed(records):
        line = json.dumps(
            {
                "id": r.id,
                "workdir": r.workdir,
                "base": r.base_profile,
                "dynamic": r.dynamic,
                "success": r.success,
                "task": r.task_head,
                "prompt": _cut(str(r.spec.get("prompt", ""))),
                "tools": list(r.spec.get("tools", [])),
                "skills": list(r.spec.get("skills", [])),
                "model": r.spec.get("model"),
                "context": _cut(str(r.spec.get("context", ""))),
            },
            ensure_ascii=False,
        )
        if total + len(line) + 1 > INPUT_CHARS:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(reversed(lines))


def _str_list(value) -> tuple[str, ...] | None:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    return None


def _candidate(item) -> Candidate | None:
    if not isinstance(item, dict):
        return None
    name, prompt = item.get("name"), item.get("prompt")
    if not isinstance(name, str) or not _NAME.match(name):
        return None
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    tools = _str_list(item.get("tools", []))
    skills = _str_list(item.get("skills", []))
    runs = item.get("runs", [])
    if tools is None or not tools or skills is None:
        return None
    if not isinstance(runs, list) or not all(
        isinstance(i, int) and not isinstance(i, bool) for i in runs
    ):
        return None
    steps = item.get("max_steps", 30)
    if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= MAX_STEPS_CAP:
        return None
    model = item.get("model")
    description = item.get("description", name)
    context = item.get("context", "")
    if model is not None and not isinstance(model, str):
        return None
    if not isinstance(description, str) or not isinstance(context, str):
        return None
    return Candidate(
        name, description, prompt, tools, skills, model or None, context, steps, tuple(runs)
    )


def parse_candidates(text: str) -> list[Candidate]:
    """Candidates from the model's reply; anything malformed is dropped, never raised."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return []
    items = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [c for c in map(_candidate, items) if c is not None]


def check_candidate(
    candidate: Candidate,
    runs_by_id: Mapping[int, RunRecord],
    *,
    taken: Mapping[str, bool],
    allow_write: bool,
) -> Verdict:
    """Apply the thresholds and the rights ceiling; the model's word is not enough."""
    ids = tuple(sorted({i for i in candidate.runs if i in runs_by_id}))
    runs = [runs_by_id[i] for i in ids]
    successes = sum(r.success for r in runs)
    workdirs = tuple(sorted({r.workdir for r in runs}))
    used_tools = {t for r in runs for t in r.spec.get("tools", [])}
    used_skills = {s for r in runs for s in r.spec.get("skills", [])}
    mutating = {t for r in runs for t in r.mutating}
    writes = any(t in WRITE_TOOLS or t in mutating for t in candidate.tools)

    def verdict(reason: str = "") -> Verdict:
        return Verdict(candidate, not reason, reason, ids, successes, workdirs, writes)

    if len(runs) < MIN_RUNS:
        return verdict(f"only {len(runs)} runs (needs {MIN_RUNS})")
    if successes / len(runs) < MIN_SUCCESS:
        return verdict(f"{successes}/{len(runs)} succeeded (needs {int(MIN_SUCCESS * 100)} %)")
    forbidden = sorted(set(candidate.tools) & CHILD_FORBIDDEN)
    if forbidden:
        return verdict(f"subagents cannot use {', '.join(forbidden)}")
    unknown = sorted(set(candidate.tools) - used_tools)
    if unknown:
        return verdict(f"tools never used by its runs: {', '.join(unknown)}")
    unknown_skills = sorted(set(candidate.skills) - used_skills)
    if unknown_skills:
        return verdict(f"skills never used by its runs: {', '.join(unknown_skills)}")
    if taken.get(candidate.name) is False:
        return verdict(f"name {candidate.name!r} belongs to a hand-written profile")
    if writes and not allow_write:
        return verdict("it writes, but [subagents] allow_write = false")
    return verdict()


async def propose(provider: Provider, model: str, records: Sequence[RunRecord]) -> list[Candidate]:
    """Ask ``model`` once for candidate profiles (no request for an empty journal)."""
    if not records:
        return []
    request = ModelRequest(
        model=model,
        system=DISTILL_PROMPT,
        messages=[Message.user(render_journal(records))],
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    acc = StreamAccumulator(provider.name)
    try:
        async for event in provider.stream(request):
            acc.feed(event)
    except ProviderError as exc:
        raise DistillError(str(exc)) from exc
    return parse_candidates(acc.message().text)


def should_disable(recent: Sequence[RunRecord]) -> bool:
    """Return True when an auto profile's newest runs mostly fail (``recent``: newest first)."""
    window = list(recent)[:DISABLE_WINDOW]
    if len(window) < DISABLE_MIN_RUNS:
        return False
    return sum(r.success for r in window) / len(window) < DISABLE_BELOW


__all__ = [
    "DISTILL_PROMPT",
    "Candidate",
    "DistillError",
    "Verdict",
    "check_candidate",
    "parse_candidates",
    "propose",
    "render_journal",
    "should_disable",
]
