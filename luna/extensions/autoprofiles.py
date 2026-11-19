"""Self-improving subagent profiles: distill the journal into ``subagents.toml``.

Policy lives here, mechanics in ``luna_core.assemblies`` / ``luna_core.distill``:
where a profile is saved, versions and undo, auto-disable, and the hour gate
that keeps the analysis to one request an hour (the API-key rule).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
from luna_core.filelock import file_lock
from luna_core.subagents import BUILTIN_SUBAGENTS
from tomlkit.exceptions import ParseError
from tomlkit.items import Table

from luna.config.config import config_dir

HOUR = 3600
WEEK = 7 * 86400
EVERY = 20
_LAST_TS = "distill_last_ts"
_LAST_ID = "distill_last_id"
_JOURNALS: dict[str, AssemblyLog] = {}
_TASKS: set[asyncio.Task] = set()
_log = logging.getLogger(__name__)


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
    """Hand every queued notice to ``emit(level, text)``; returns how many.

    When ``emit`` raises, the notices it did not take go back to the queue
    and the error propagates.
    """
    notices = journal.drain_notices()
    for i, (level, text) in enumerate(notices):
        try:
            emit(level, text)
        except BaseException:
            for rest in notices[i:]:
                journal.push_notice(*rest)
            raise
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


def report_lines(journal: AssemblyLog, report: DistillReport) -> list[str]:
    """Return the report plus every other queued notice (all are shown, the queue empties)."""
    out = report.lines()
    deliver_notices(journal, lambda level, text: None if text in report.saved else out.append(text))
    return out


class _FileError(Exception):
    """A subagents.toml could not be read or written."""


def _read_doc(path: Path) -> tomlkit.TOMLDocument:
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        doc = tomlkit.parse(text)
    except (OSError, UnicodeDecodeError, ParseError) as exc:
        raise _FileError(f"cannot read {path}: {exc}") from exc
    # profiles are written as [subagent.<name>] tables; anything else is not ours to edit
    if "subagent" in doc and not isinstance(doc["subagent"], Table):
        raise _FileError(f"cannot read {path}: 'subagent' must be a table")
    return doc


def _target(path: Path) -> Path:
    """Return the real file behind ``path`` (a dotfiles symlink is followed, not replaced)."""
    return path.resolve() if path.is_symlink() else path


@contextlib.contextmanager
def _editing(path: Path):
    """Hold the cross-process lock of ``path`` for one read-modify-write.

    The server's background analysis and a ``luna subagents`` command in
    another process may edit the same file; without the lock one would
    overwrite the other's change.
    """
    target = _target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(target.with_name(f".{target.name}.lock")):
        yield


def _write_doc(path: Path, doc: tomlkit.TOMLDocument) -> None:
    target = _target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, target.stat().st_mode)  # keep the user's permissions
        os.replace(tmp, target)
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
        """Names in use -> True when Luna may overwrite them (auto profiles).

        A project file is loaded after the user file and wins, so an auto
        profile there would shadow a hand-written user profile: both files
        count for a project target.
        """
        taken = {name: False for name in BUILTIN_SUBAGENTS}
        layers = [self.user_file()]
        if path != self.user_file():
            layers.append(path)
        for layer in layers:
            for name, cfg in _profiles(_read_doc(layer)).items():
                auto = isinstance(cfg, dict) and cfg.get("auto") is True
                taken[name] = taken.get(name, True) and auto
        return taken

    def _save(self, verdict: Verdict, path: Path, runs: Mapping[int, RunRecord]) -> None:
        name = verdict.candidate.name
        with _editing(path):
            doc = _read_doc(path)
            prev = _plain(_profiles(doc).get(name))
            if prev is not None and prev.get("auto") is not True:
                raise _FileError(f"{name!r} became a hand-written profile in {path}")
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
        recent = self._recent_runs(name, path)
        if not should_disable(recent):
            return None
        with _editing(path):
            try:
                doc = _read_doc(path)
            except _FileError:
                return None
            current = _plain(_profiles(doc).get(name))
            if not current or current.get("auto") is not True or current.get("disabled") is True:
                return None
            data = dict(current, disabled=True)
            _set_profile(doc, name, data)
            _write_doc(path, doc)
        window = recent[:10]
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

    def _recent_runs(self, name: str, path: Path) -> list[RunRecord]:
        """Return the runs of the profile as it is now: the auto-disable window.

        Only runs recorded after its latest save / update / enable / undo
        in ``path`` (an older version's failures do not count against it),
        only runs of the profile itself (not dynamic assemblies based on
        it), and for a project profile only runs in that project.
        """
        since = next(
            (
                v.ts
                for v in self._journal.versions(name, 200)
                if v.path == str(path) and v.action != "disable"
            ),
            None,
        )
        workdir = None if path == self.user_file() else str(path.parent.parent)
        return self._journal.recent_for(name, after_ts=since, dynamic=False, workdir=workdir)

    def set_disabled(self, name: str, disabled: bool, *, workdir: str) -> str:
        """Turn a profile from a subagents.toml off or back on."""
        for path in (self.project_file(workdir), self.user_file()):
            if not path.exists():
                continue
            with _editing(path):
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
        with _editing(path):
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


def make_on_recorded(auto: AutoProfiles, *, fast: tuple[object, str], allow_write: bool):
    """Build the runner's ``on_recorded``: auto-disable check, then the every-``EVERY`` trigger."""

    def on_recorded(record: RunRecord, session) -> None:
        if auto._journal.versions(record.base_profile, 1):
            auto.check_disable(record.base_profile)
        if _TASKS or not auto.due_by_count() or auto.wait_left() > 0:
            return
        task = asyncio.get_running_loop().create_task(
            auto.distill(fast[0], fast[1], allow_write=allow_write)
        )
        _TASKS.add(task)
        task.add_done_callback(_done)

    return on_recorded


def _done(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        exc = task.exception()
        _log.warning("background distill failed: %s", exc, exc_info=exc)


async def stop_background() -> None:
    """Cancel the running background analyses and wait for them (server shutdown)."""
    tasks = list(_TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def fast_target(config) -> tuple[object, str]:
    """Return the provider and model of the ``fast`` role (raises LunaConfigError)."""
    from luna.config.providers import build_provider, merge_providers
    from luna.core.agent import _roles

    registry = merge_providers(config.custom_providers)
    provider, model = build_provider(config.provider, config.model, registry=registry)
    return _roles(config, registry).get("fast", provider, model)


async def weekly_distill(config) -> None:
    """Server start: run the analysis when a week has passed and there is something new."""
    from luna.config.providers import LunaConfigError

    try:
        auto = AutoProfiles(open_journal())
        if not auto.due_weekly():
            return
        fast = fast_target(config)
        await auto.distill(*fast, allow_write=config.subagents_allow_write)
    except LunaConfigError as exc:
        _log.warning("weekly distill skipped: %s", exc)
    except Exception:  # noqa: BLE001 - never take the server down
        _log.exception("weekly distill failed")


__all__ = [
    "EVERY",
    "HOUR",
    "WEEK",
    "AutoProfiles",
    "DistillReport",
    "close_journals",
    "deliver_notices",
    "fast_target",
    "make_on_recorded",
    "open_journal",
    "report_lines",
    "stop_background",
    "weekly_distill",
]
