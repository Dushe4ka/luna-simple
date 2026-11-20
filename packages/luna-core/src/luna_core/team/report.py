"""The run report: ``.luna/teams/<id>/report.md`` and the same data as a dict."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from luna_core.team.board import TeamEvent, TeamRun, TeamStore, TeamTask

OUTCOME_TITLES = {
    "done": "ГОТОВО",
    "partial": "ЧАСТИЧНО",
    "stopped": "ОСТАНОВЛЕНО",
    "stalled": "ЗАСТОЙ",
    "error": "ОШИБКА",
}
STOP_REASONS = {"time": "время", "budget": "бюджет", "cancel": "отмена"}
STATUS_TITLES = {
    "pending": "ждёт",
    "in_progress": "в работе",
    "done": "готово",
    "failed": "провал",
    "blocked": "заблокирована",
}
PROBLEM_KINDS = frozenset(
    {"team_denied", "verify_failed", "conflict", "markers", "interrupted", "provider_error",
     "stall_warn", "stalled", "lead_out_of_steps", "merge_undone", "error"}
)  # fmt: skip
#: How much of each research note the report quotes (characters).
NOTE_LIMIT = 2000


@dataclass(frozen=True)
class Report:
    """The rendered report and its data (for ``luna team show`` and the TUI)."""

    markdown: str
    data: dict


def headline(run: TeamRun, tasks: list[TeamTask]) -> str:
    """``ГОТОВО`` | ``ЧАСТИЧНО n/m`` | ``ОСТАНОВЛЕНО (…)`` | ``ЗАСТОЙ`` | ``ОШИБКА``."""
    outcome = run.outcome or "error"
    if outcome == "partial":
        done = sum(t.status == "done" for t in tasks)
        return f"ЧАСТИЧНО {done}/{len(tasks)}"
    if outcome == "stopped":
        reason = STOP_REASONS.get(run.stop_reason or "", run.stop_reason or "?")
        return f"ОСТАНОВЛЕНО ({reason})"
    return OUTCOME_TITLES.get(outcome, OUTCOME_TITLES["error"])


def _cell(text: object) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _line(text: object) -> str:
    return " ".join(str(text).split())


def _minutes(seconds: float) -> str:
    whole = int(seconds // 60)
    return f"{whole} мин" if whole else f"{int(seconds)} с"


def _problem(event: TeamEvent) -> str:
    d = event.data
    if event.kind == "team_denied":
        why = "запрещено правилом permissions.toml" if d.get("reason") == "rule" else "не в профиле"
        return f"⊘ {event.member}: `{_line(d.get('preview', ''))}` — {why}"
    if event.kind == "verify_failed":
        return f"✗ {d.get('task')}: проверка не прошла после вливания (попытка {d.get('attempt')})"
    if event.kind == "conflict":
        return f"⚠ {d.get('task')}: конфликт при вливании → {d.get('fix')}"
    if event.kind == "markers":
        return f"⚠ {d.get('task')}: в файлах остались маркеры конфликта ({d.get('files')})"
    if event.kind == "interrupted":
        return f"↻ прервано в {d.get('at')}, продолжено"
    if event.kind == "provider_error":
        return f"✗ {event.member}: ошибка провайдера: {_line(d.get('message', ''))}"
    if event.kind == "stall_warn":
        return "… доска не менялась несколько пробуждений подряд — лидеру сказано действовать"
    if event.kind == "stalled":
        return "… застой: доска так и не изменилась, запуск остановлен"
    if event.kind == "lead_out_of_steps":
        return "… у лидера кончились шаги"
    if event.kind == "merge_undone":
        undone = "откачено, " if d.get("undone", True) else ""
        return f"↺ {d.get('task')}: вливание прервано до проверки — {undone}задача повторяется"
    return f"✗ {_line(d.get('message', ''))}"


def read_notes(notes_dir: str | Path | None) -> list[dict]:
    """Read the research notes in ``notes_dir`` (``<task>.md``), each cut to ``NOTE_LIMIT``."""
    if notes_dir is None or not Path(notes_dir).is_dir():
        return []
    notes = []
    for path in sorted(Path(notes_dir).glob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        title, _, body = raw.partition("\n") if raw.startswith("# ") else ("", "", raw)
        body = body.strip()
        cut = body[:NOTE_LIMIT] + ("…" if len(body) > NOTE_LIMIT else "")
        notes.append({"task": path.stem, "title": title[2:].strip() or path.stem, "text": cut})
    return notes


def build_report(
    store: TeamStore,
    run_id: str,
    *,
    diffstat: str = "",
    profile_changes: Iterable[str] = (),
    notes_dir: str | Path | None = None,
) -> Report:
    """Render the report of a run from the board, its events and its research notes."""
    run = store.run(run_id)
    if run is None:
        raise KeyError(f"no team run {run_id}")
    tasks = store.tasks(run_id)
    members = store.members(run_id)
    events = store.events(run_id)
    title = headline(run, tasks)
    problems = [_problem(e) for e in events if e.kind in PROBLEM_KINDS]
    decisions = [_line(e.data.get("text", "")) for e in events if e.kind == "decision"]
    changes = [_line(c) for c in profile_changes]
    notes = read_notes(notes_dir)
    cost = sum(m.cost for m in members)
    duration = ((run.finished or time.time()) - run.started) if run.started else 0.0
    merge = f"git merge --no-ff {run.branch}" if run.branch else ""

    lines = [f"# Команда {run.id}", "", f"**Задача:** {_line(run.task)}", ""]
    if run.plan and run.plan.get("goal"):
        lines += [f"**Цель:** {_line(run.plan['goal'])}", ""]
    lines += [f"**Итог:** {title}", "", f"**Время:** {_minutes(duration)} · "
              f"**Стоимость:** ${cost:.2f}", "", "## Задачи", ""]  # fmt: skip
    if tasks:
        lines += [
            "| | Задача | Статус | Кто | Результат | Коммиты |",
            "|---|---|---|---|---|---|",
        ]
        for t in tasks:
            commits = ", ".join(c[:7] for c in t.commits) or "—"
            lines.append(
                f"| {t.id} | {_cell(t.title)} | {STATUS_TITLES[t.status]} | {t.assignee or '—'} "
                f"| {_cell(t.note[:200]) or '—'} | {commits} |"
            )
    else:
        lines.append("Задач нет.")
    if notes:
        lines += ["", "## Заметки"]
        for note in notes:
            lines += ["", f"### {_line(note['title'])}", "", note["text"] or "—"]
    undone = [t for t in tasks if t.status != "done"]
    lines += ["", "## Не сделано", ""]
    lines += [
        f"- {t.id} {_line(t.title)} — {STATUS_TITLES[t.status]}: "
        f"{_line(t.note[:300]) or 'без пояснения'}"
        for t in undone
    ] or ["Всё сделано."]
    lines += ["", "## Что пошло не так", ""] + ([f"- {p}" for p in problems] or ["Ничего."])
    lines += ["", "## Решения лидера", ""] + ([f"- {d}" for d in decisions] or ["Нет."])
    lines += ["", "## Участники", "", "| Участник | Шаги | Стоимость | Время работы |",
              "|---|---|---|---|"]  # fmt: skip
    lines += [f"| {m.name} | {m.steps} | ${m.cost:.2f} | {_minutes(m.busy)} |" for m in members]
    if run.branch:
        lines += ["", "## Ветка", "", f"`{run.branch}` (от `{(run.base or '')[:7]}`)", ""]
        if diffstat:
            lines += ["```", diffstat, "```", ""]
        lines.append(f"Слить в свою ветку: `{merge}`")
    if changes:
        lines += ["", "## Профили субагентов", ""] + [f"- {c}" for c in changes]

    data = {
        "id": run.id,
        "task": run.task,
        "outcome": run.outcome,
        "stop_reason": run.stop_reason,
        "headline": title,
        "branch": run.branch,
        "base": run.base,
        "merge_command": merge,
        "diffstat": diffstat,
        "cost": cost,
        "duration": duration,
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "kind": t.kind,
                "status": t.status,
                "assignee": t.assignee,
                "note": t.note,
                "commits": list(t.commits),
            }
            for t in tasks
        ],  # fmt: skip
        "members": [
            {"name": m.name, "steps": m.steps, "cost": m.cost, "busy": m.busy} for m in members
        ],
        "problems": problems,
        "decisions": decisions,
        "profile_changes": changes,
        "notes": notes,
    }
    return Report("\n".join(lines) + "\n", data)


def write_report(root: str | Path, run_id: str, report: Report) -> Path:
    """Write ``.luna/teams/<id>/report.md`` under ``root`` and return its path."""
    path = Path(root) / ".luna" / "teams" / run_id / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.markdown, encoding="utf-8")
    return path
