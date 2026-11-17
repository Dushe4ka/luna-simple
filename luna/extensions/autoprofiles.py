"""Self-improving subagent profiles: distill the journal into ``subagents.toml``.

Policy lives here, mechanics in ``luna_core.assemblies`` / ``luna_core.distill``:
where a profile is saved, versions and undo, auto-disable, and the hour gate
that keeps the analysis to one request an hour (the API-key rule).
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import tomlkit
from luna_core.assemblies import AssemblyLog, ProfileVersion, RunRecord
from luna_core.distill import DistillError, Verdict, check_candidate, propose, should_disable
from luna_core.subagents import BUILTIN_SUBAGENTS
from tomlkit.exceptions import ParseError

from luna.config.config import config_dir

HOUR = 3600
WEEK = 7 * 86400
EVERY = 20
_LAST_TS = "distill_last_ts"
_LAST_ID = "distill_last_id"
_JOURNALS: dict[str, AssemblyLog] = {}


def open_journal(env: Mapping[str, str] | None = None) -> AssemblyLog:
    """Return the process-wide journal in ``sessions.db``."""
    from luna.core.store import db_path

    key = str(db_path(env))
    if key not in _JOURNALS:
        _JOURNALS[key] = AssemblyLog(key)
    return _JOURNALS[key]


def close_journals() -> None:
    """Close every cached journal (tests switch config homes)."""
    for journal in _JOURNALS.values():
        journal.close()
    _JOURNALS.clear()


def deliver_notices(journal: AssemblyLog, emit: Callable[[str, str], None]) -> int:
    """Hand every queued notice to ``emit(level, text)``; returns how many."""
    notices = journal.drain_notices()
    for level, text in notices:
        emit(level, text)
    return len(notices)


@dataclass
class DistillReport:
    """What one analysis did."""

    saved: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    skipped: str = ""

    def lines(self) -> list[str]:
        """Human-readable summary."""
        if self.skipped:
            return [self.skipped]
        out = list(self.saved)
        out += [f"not saved: {name} — {reason}" for name, reason in self.rejected]
        return out or ["no repeating assemblies found"]


class _FileError(Exception):
    """A subagents.toml could not be read or written."""


def _read_doc(path: Path) -> tomlkit.TOMLDocument:
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return tomlkit.parse(text)
    except (OSError, UnicodeDecodeError, ParseError) as exc:
        raise _FileError(f"cannot read {path}: {exc}") from exc


def _write_doc(path: Path, doc: tomlkit.TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _profiles(doc: tomlkit.TOMLDocument) -> dict:
    table = doc.get("subagent")
    return table if isinstance(table, dict) else {}


def _plain(value) -> dict | None:
    return None if value is None else dict(value.unwrap() if hasattr(value, "unwrap") else value)


def _set_profile(doc: tomlkit.TOMLDocument, name: str, data: dict | None) -> None:
    if "subagent" not in doc:
        doc["subagent"] = tomlkit.table(is_super_table=True)
    if data is None:
        doc["subagent"].pop(name, None)
        return
    table = tomlkit.table()
    for key, value in data.items():
        table[key] = value
    doc["subagent"][name] = table


def _profile_data(verdict: Verdict, version: int, runs: Mapping[int, RunRecord]) -> dict:
    c = verdict.candidate
    seen_models = {runs[i].spec.get("model") for i in verdict.runs if i in runs}
    data: dict = {"description": c.description, "prompt": c.prompt, "tools": list(c.tools)}
    if c.skills:
        data["skills"] = list(c.skills)
    if c.model and c.model in seen_models:
        data["model"] = c.model
    if c.context:
        data["context"] = c.context
    data["max_steps"] = c.max_steps
    if verdict.writes:
        data["unsafe"] = True
    data["auto"] = True
    data["version"] = version
    return data


class AutoProfiles:
    """Distill, save, version, undo and disable auto profiles."""

    def __init__(
        self,
        journal: AssemblyLog,
        *,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._journal = journal
        self._env = env
        self._clock = clock

    def user_file(self) -> Path:
        """Return the user-level subagents.toml."""
        return config_dir(self._env) / "subagents.toml"

    @staticmethod
    def project_file(workdir: str) -> Path:
        """Return the project-level subagents.toml of ``workdir``."""
        return Path(workdir) / ".luna" / "subagents.toml"

    # --- when ---------------------------------------------------------------

    def wait_left(self) -> float:
        """Return seconds left in the hour window (0 when an analysis may run)."""
        last = self._journal.get_meta(_LAST_TS)
        if last is None:
            return 0.0
        return max(0.0, float(last) + HOUR - self._clock())

    def due_by_count(self) -> bool:
        """Return True when enough runs piled up since the last analysis."""
        last = int(self._journal.get_meta(_LAST_ID) or 0)
        return self._journal.count_after(last) >= EVERY

    def due_weekly(self) -> bool:
        """Return True when runs are pending and a week passed since the last analysis."""
        if not self._journal.pending(1):
            return False
        last = self._journal.get_meta(_LAST_TS)
        return last is None or self._clock() - float(last) >= WEEK

    def _claim(self) -> bool:
        now = self._clock()
        prev = self._journal.get_meta(_LAST_TS)
        if prev is not None and now - float(prev) < HOUR:
            return False
        return self._journal.claim_meta(_LAST_TS, expect=prev, value=repr(now))

    # --- distill ------------------------------------------------------------

    async def distill(self, provider, model: str, *, allow_write: bool) -> DistillReport:
        """One analysis: at most one request, at most once an hour."""
        report = DistillReport()
        claimed = await asyncio.to_thread(self._claim)
        if not claimed:
            minutes = max(1, math.ceil(self.wait_left() / 60))
            report.skipped = f"analysis ran recently; next in {minutes} min"
            return report
        records = self._journal.pending(200)
        self._journal.set_meta(_LAST_ID, str(self._journal.last_id()))
        if not records:
            report.skipped = "nothing new in the journal"
            return report
        try:
            candidates = await propose(provider, model, records)
        except DistillError as exc:
            report.skipped = f"analysis failed: {exc}"
            return report
        runs_by_id = {r.id: r for r in records}
        saved_names: set[str] = set()
        used_runs: set[int] = set()
        for candidate in candidates:
            if candidate.name in saved_names:
                report.rejected.append((candidate.name, "duplicate name in this analysis"))
                continue
            free = tuple(i for i in candidate.runs if i not in used_runs)
            candidate = replace(candidate, runs=free)
            first = check_candidate(candidate, runs_by_id, taken={}, allow_write=allow_write)
            if not first.workdirs:
                report.rejected.append((candidate.name, first.reason or "no runs"))
                continue
            path = (
                self.user_file()
                if len(first.workdirs) >= 2
                else self.project_file(first.workdirs[0])
            )
            try:
                taken = self._taken(path)
            except _FileError as exc:
                report.rejected.append((candidate.name, str(exc)))
                continue
            verdict = check_candidate(candidate, runs_by_id, taken=taken, allow_write=allow_write)
            if not verdict.ok:
                report.rejected.append((candidate.name, verdict.reason))
                continue
            try:
                self._save(verdict, path, runs_by_id)
            except (_FileError, OSError) as exc:
                report.rejected.append((candidate.name, str(exc)))
                continue
            text = (
                f"Luna сохранила профиль `{candidate.name}` "
                f"({verdict.successes}/{len(verdict.runs)} успешно)"
            )
            self._journal.push_notice("info", text)
            report.saved.append(text)
            saved_names.add(candidate.name)
            used_runs.update(verdict.runs)
        return report

    def _taken(self, path: Path) -> dict[str, bool]:
        taken = {name: False for name in BUILTIN_SUBAGENTS}
        for name, cfg in _profiles(_read_doc(path)).items():
            taken[name] = bool(isinstance(cfg, dict) and cfg.get("auto") is True)
        return taken

    def _save(self, verdict: Verdict, path: Path, runs: Mapping[int, RunRecord]) -> None:
        name = verdict.candidate.name
        doc = _read_doc(path)
        prev = _plain(_profiles(doc).get(name))
        version = int(prev.get("version", 1)) + 1 if prev else 1
        data = _profile_data(verdict, version, runs)
        _set_profile(doc, name, data)
        _write_doc(path, doc)
        self._journal.add_version(
            name=name,
            path=str(path),
            action="update" if prev else "save",
            version=version,
            spec=data,
            prev=prev,
            note=f"{verdict.successes}/{len(verdict.runs)} runs succeeded",
        )
        self._journal.mark(verdict.runs, name)

    # --- disable / undo / history -------------------------------------------

    def check_disable(self, name: str) -> str | None:
        """Turn off an auto profile whose recent runs mostly fail."""
        latest = self._journal.versions(name, 1)
        if not latest:
            return None
        path = Path(latest[0].path)
        try:
            doc = _read_doc(path)
        except _FileError:
            return None
        current = _plain(_profiles(doc).get(name))
        if not current or current.get("auto") is not True or current.get("disabled") is True:
            return None
        recent = self._journal.recent_for(name)
        if not should_disable(recent):
            return None
        window = recent[:10]
        data = dict(current, disabled=True)
        _set_profile(doc, name, data)
        _write_doc(path, doc)
        self._journal.add_version(
            name=name,
            path=str(path),
            action="disable",
            version=int(current.get("version", 1)),
            spec=data,
            prev=current,
            note="auto: low success",
        )
        good = sum(r.success for r in window)
        text = (
            f"Luna отключила профиль `{name}`: из последних {len(window)} запусков успешны {good}"
        )
        self._journal.push_notice("warn", text)
        return text

    def set_disabled(self, name: str, disabled: bool, *, workdir: str) -> str:
        """Turn a profile from a subagents.toml off or back on."""
        for path in (self.project_file(workdir), self.user_file()):
            try:
                doc = _read_doc(path)
            except _FileError as exc:
                return str(exc)
            current = _plain(_profiles(doc).get(name))
            if current is None:
                continue
            data = dict(current)
            if disabled:
                data["disabled"] = True
            else:
                data.pop("disabled", None)
            _set_profile(doc, name, data)
            _write_doc(path, doc)
            self._journal.add_version(
                name=name,
                path=str(path),
                action="disable" if disabled else "enable",
                version=int(current.get("version", 1)),
                spec=data,
                prev=current,
            )
            return f"{name}: {'disabled' if disabled else 'enabled'} ({path})"
        return (
            f"profile {name!r} is not defined in a subagents.toml "
            "(built-in profiles cannot be disabled here)"
        )

    def undo(self, name: str) -> str:
        """Roll back the newest change of ``name`` that is not undone yet."""
        target: ProfileVersion | None = next(
            (v for v in self._journal.versions(name, 200) if v.action != "undo" and not v.undone),
            None,
        )
        if target is None:
            return f"nothing to undo for {name}"
        path = Path(target.path)
        try:
            doc = _read_doc(path)
        except _FileError as exc:
            return str(exc)
        current = _plain(_profiles(doc).get(name))
        if current != target.spec:
            return f"{name} was edited by hand after that change; nothing was undone"
        _set_profile(doc, name, target.prev)
        _write_doc(path, doc)
        self._journal.set_undone(target.id)
        restored = int(target.prev.get("version", 1)) if target.prev else 0
        self._journal.add_version(
            name=name,
            path=str(path),
            action="undo",
            version=restored,
            spec=target.prev,
            prev=target.spec,
        )
        if target.prev is None:
            return f"{name}: removed"
        return f"{name}: restored version {restored}"

    def history(self, name: str | None = None) -> list[str]:
        """Profile changes, newest first."""
        out = []
        for v in self._journal.versions(name):
            when = datetime.fromtimestamp(v.ts).strftime("%Y-%m-%d %H:%M")
            mark = " (undone)" if v.undone else ""
            out.append(f"{when}  {v.action:<7} {v.name} v{v.version}  {v.path}{mark}")
        return out


__all__ = [
    "EVERY",
    "HOUR",
    "WEEK",
    "AutoProfiles",
    "DistillReport",
    "close_journals",
    "deliver_notices",
    "open_journal",
]
