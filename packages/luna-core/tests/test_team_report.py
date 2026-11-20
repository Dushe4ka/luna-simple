"""The team run report."""

import pytest
from luna_core.team.board import LEAD, TeamStore
from luna_core.team.report import build_report, write_report


@pytest.fixture
def store(tmp_path):
    s = TeamStore(tmp_path / "s.db")
    yield s
    s.close()


def finished(store, outcome, reason=None):
    rid = store.create_run(workdir="/w", task="add flag", session_id="s", settings={})
    store.update_run(
        rid, status="finished", outcome=outcome, stop_reason=reason, started=100.0, finished=400.0
    )
    return rid


@pytest.mark.parametrize(
    ("outcome", "reason", "expected"),
    [
        ("done", None, "ГОТОВО"),
        ("partial", None, "ЧАСТИЧНО 1/3"),
        ("stopped", "budget", "ОСТАНОВЛЕНО (бюджет)"),
        ("stopped", "time", "ОСТАНОВЛЕНО (время)"),
        ("stopped", "cancel", "ОСТАНОВЛЕНО (отмена)"),
        ("stalled", None, "ЗАСТОЙ"),
        ("error", None, "ОШИБКА"),
    ],
)
def test_headline_for_every_outcome(store, outcome, reason, expected):
    rid = finished(store, outcome, reason)
    for title in ("a", "b", "c"):
        store.add_task(rid, title=title)
    store.update_task(rid, "T1", status="done")
    report = build_report(store, rid)
    assert report.data["headline"] == expected
    assert f"**Итог:** {expected}" in report.markdown


def test_report_lists_tasks_problems_decisions_and_cost(store, tmp_path):
    rid = finished(store, "partial")
    store.update_run(rid, branch="luna/team/x", base="abcdef123", plan={"goal": "flag"})
    store.add_member(rid, LEAD, {"role": "lead"})
    store.add_member(rid, "dev", {})
    store.update_member(rid, "dev", add_steps=7, add_cost=1.25, add_busy=120)
    store.add_task(rid, title="impl")
    store.update_task(
        rid, "T1", status="done", assignee="dev", note="wrote it", commits=["1234567890"]
    )
    store.add_task(rid, title="docs | api")
    store.update_task(rid, "T2", status="failed", note="ran out of steps")
    store.log_event(
        rid, "team_denied", "dev", tool="execute", preview="curl https://x", reason="profile"
    )
    store.log_event(rid, "team_denied", "dev", tool="execute", preview="git push", reason="rule")
    store.log_event(rid, "verify_failed", "dev", task="T1", attempt=1)
    store.log_event(rid, "conflict", "dev", task="T1", fix="T3")
    store.log_event(rid, "interrupted", at="12:30")
    store.log_event(rid, "decision", LEAD, text="split T2")
    report = build_report(
        store, rid, diffstat=" b.txt | 1 +", profile_changes=["auto-profile tester v1: create"]
    )
    md = report.markdown
    assert "**Итог:** ЧАСТИЧНО 1/2" in md and "**Цель:** flag" in md
    assert "| T1 | impl | готово | dev | wrote it | 1234567 |" in md
    assert "| T2 | docs \\| api | провал | — | ran out of steps | — |" in md
    assert "- T2 docs | api — провал: ran out of steps" in md
    assert "⊘ dev: `curl https://x` — не в профиле" in md
    assert "⊘ dev: `git push` — запрещено правилом permissions.toml" in md
    assert "✗ T1: проверка не прошла после вливания (попытка 1)" in md
    assert "⚠ T1: конфликт при вливании → T3" in md
    assert "↻ прервано в 12:30, продолжено" in md
    assert "- split T2" in md
    assert "| dev | 7 | $1.25 | 2 мин |" in md and "5 мин" in md
    assert "`luna/team/x` (от `abcdef1`)" in md and " b.txt | 1 +" in md
    assert "git merge --no-ff luna/team/x" in md
    assert "- auto-profile tester v1: create" in md
    assert report.data["cost"] == 1.25 and report.data["merge_command"].endswith("luna/team/x")
    path = write_report(tmp_path, rid, report)
    assert path == tmp_path / ".luna" / "teams" / rid / "report.md"
    assert path.read_text(encoding="utf-8") == md


def test_quiet_run_says_so(store):
    rid = finished(store, "done")
    store.add_task(rid, title="a")
    store.update_task(rid, "T1", status="done")
    md = build_report(store, rid).markdown
    assert "Всё сделано." in md and "Ничего." in md and "Нет." in md
    assert "## Ветка" not in md and "## Профили субагентов" not in md


def test_research_notes_go_into_the_report(store, tmp_path):
    rid = finished(store, "done")
    store.add_task(rid, title="look around", kind="research")
    notes = tmp_path / "notes"
    notes.mkdir()
    note = "# T1: look around\n\nThe CLI lives in cli.py.\n"
    (notes / "T1.md").write_text(note, encoding="utf-8")
    (notes / "T2.md").write_text("x" * 5000, encoding="utf-8")
    report = build_report(store, rid, notes_dir=notes)
    assert "## Заметки" in report.markdown
    assert "The CLI lives in cli.py." in report.markdown
    assert "x" * 2000 in report.markdown and "x" * 2001 not in report.markdown
    assert [n["task"] for n in report.data["notes"]] == ["T1", "T2"]
    assert "## Заметки" not in build_report(store, rid).markdown
    assert "## Заметки" not in build_report(store, rid, notes_dir=tmp_path / "none").markdown
