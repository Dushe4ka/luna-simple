# Luna: журнал сборок и автопрофили (план 1b) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Каждый запуск субагента пишется в журнал; периодический анализ (`distill`) одним запросом к роли `fast` находит повторяющиеся удачные сборки и сам сохраняет их как auto-профили в `subagents.toml`, с историей, откатом и автоотключением.

**Architecture:** В `luna-core` два новых модуля: `assemblies.py` (SQLite-журнал в том же `sessions.db`: запуски, версии профилей, отложенные уведомления, метаданные) и `distill.py` (рендер журнала в запрос, разбор JSON-ответа, проверка кандидатов по порогам, решение об автоотключении). `SubagentRunner` получает поле `journal` и колбэк `on_recorded`. В приложении `luna/extensions/autoprofiles.py` связывает это с файлами: пишет TOML через `tomlkit`, ведёт версии, откат, `disable/enable`, часовой лимит. Триггеры: каждые 20 записей (фоново из `on_recorded`), раз в неделю при старте сервера, вручную через `luna subagents distill` и `/subagents distill`.

**Tech Stack:** Python 3.11+, `sqlite3` (WAL), `tomlkit` (уже в зависимостях), `luna_core.providers.fake.FakeProvider` в тестах, pytest + pytest-asyncio (asyncio_mode=auto, как в репо).

**Spec:** `docs/superpowers/specs/2026-11-16-luna-dynamic-subagents-teams-design.md`, часть 1b (разделы 1b.1, 1b.2, «Тесты → Часть 1b», «Риски»).

## Global Constraints

- Ветка: `feat/auto-profiles` от `main` (df71fc7). В `main` и в origin — только по слову пользователя.
- Python 3.11+, PEP 8 / PEP 257, чистый `uv run ruff check .` и `uv run ruff format --check .`.
- Тесты не ходят в сеть: только `FakeProvider`. Реальные ключи в тестах и CI не используются никогда.
- ID моделей — только в конфиге и `luna/config/providers.py`; анализ берёт модель роли `fast`.
- Журнал: `spec_json` хранит промпт, инструменты, скилы, модель и `context` — то, что написал родитель. Вывод инструментов и ответы субагента туда **не** попадают. `task_head` — первые 200 символов задачи.
- Успех = `ok и не cancelled и не hit_max_steps`.
- Пороги сохранения: ≥ 5 запусков в группе и ≥ 70 % успешных.
- Анализ берёт записи без `profile_name`, последние 200 штук.
- Анализ: один запрос за раз и не чаще одного раза в час — для всех триггеров, включая ручной (правило про ключи из CLAUDE.md).
- Автоотключение: из последних 10 запусков auto-профиля успешных меньше 50 % → `disabled = true` + уведомление.
- Ручные профили (без `auto`) и встроенные анализ никогда не меняет.
- Пишущие права auto-профиля не выше `[subagents] allow_write`.
- Куда сохранять: в пользовательский `subagents.toml`, если группа встречается в ≥ 2 `workdir`; иначе в `<workdir>/.luna/subagents.toml`.
- Уведомление о сохранении: «Luna сохранила профиль `<name>` (<успешных>/<всего> успешно)».
- Запуск после команды (`luna team`) — часть плана 2; здесь только публичный `AutoProfiles.distill`, который план 2 вызовет.

## Review Focus

1. **Два триггера одновременно** (две сессии сервера дошли до 20 записей разом) — к провайдеру уходит ровно один запрос; второй вызов получает «анализ уже шёл недавно». Тест в Task 4.
2. **Пользовательский `subagents.toml` с комментариями и ручными профилями** — сохранение auto-профиля не трогает комментарии и чужие таблицы. Тест в Task 4.
3. **Битый `subagents.toml`** (синтаксическая ошибка TOML) — кандидат для этого файла отклоняется с причиной, файл не перезаписан, анализ не падает. Тест в Task 4.
4. **Ответ модели с прозой вокруг JSON, в ```-блоке, с неверными типами или выдуманными id запусков** — валидные кандидаты разбираются, мусор отбрасывается, id не из выборки не засчитываются в пороги. Тесты в Task 3.
5. **`undo` после ручной правки профиля** — откат отказывается, файл не меняется. Тест в Task 4.

## File Structure

| Файл | Ответственность |
|---|---|
| `packages/luna-core/src/luna_core/assemblies.py` (новый) | `AssemblyLog`, `RunRecord`, `ProfileVersion`: таблицы `subagent_runs`, `subagent_profile_versions`, `luna_notices`, `subagent_meta` |
| `packages/luna-core/src/luna_core/distill.py` (новый) | `DISTILL_PROMPT`, `Candidate`, `Verdict`, `DistillError`, `render_journal`, `parse_candidates`, `check_candidate`, `propose`, `should_disable` |
| `packages/luna-core/src/luna_core/subagents.py` | поля `journal`, `on_recorded` у `SubagentRunner`; запись в `run()` |
| `packages/luna-core/src/luna_core/__init__.py` | экспорт `AssemblyLog`, `RunRecord` |
| `luna/extensions/autoprofiles.py` (новый) | `AutoProfiles`: distill-поток, запись TOML, версии, `undo`, `disable/enable`, автоотключение, часовой лимит, `open_journal`, `deliver_notices` |
| `luna/core/agent.py` | подключение журнала и `on_recorded` к раннеру |
| `luna/server/runtime.py`, `luna/server/app.py` | доставка уведомлений после хода; недельный анализ при старте |
| `luna/core/session.py` | REPL печатает накопленные уведомления |
| `luna/cli.py` | `luna subagents distill|history|undo|disable|enable` |
| `luna/commands/builtin.py` | `/subagents …` |
| `README.md`, `AGENTS.md` | описание автопрофилей, новые модули |

## Решения, принятые в плане (спека о них молчит)

- **Колонка `mutating_json`** в `subagent_runs` сверх таблицы спеки: какие инструменты сборки были пишущими/мутирующими у родителя в момент запуска. Без неё офлайн-анализ (CLI, старт сервера) не знает, мутирует ли MCP-инструмент, и не может проверить потолок `allow_write`.
- **«Инструменты существуют»** проверяется по журналу: инструмент кандидата должен встречаться в `tools` хотя бы одного запуска его группы (эти запуски прошли фильтр по инструментам родителя). Так проверка работает без живого агента.
- **Автоотключение** считается, когда у профиля ≥ 5 запусков (окно — последние до 10). Иначе сломанный профиль с 5 неудачами из 5 жил бы до 10-го запуска.
- **Уведомления** копятся в таблице `luna_notices` и доставляются после ближайшего завершённого хода (сервер: `SessionRuntime.after_turn` → `session.notify`; REPL: печать после хода и при старте), а также печатаются CLI-командой. Один путь доставки для фоновых и недельных анализов.
- **Часовой лимит** ставится (`distill_last_ts = now`) **до** запроса: неудачный запрос тоже считается попыткой. Это консервативно по нагрузке на ключ.
- **`disable/enable`** работают для профилей, описанных в `subagents.toml` (ручных и auto); для встроенных — ошибка с подсказкой. **`undo`** откатывает последнее неоткаченное изменение профиля из журнала версий и отказывается, если файл правили руками после этой версии.
- **Недельный анализ** на старте сервера берёт конфиг `load_config()` процесса сервера; если ключа или провайдера нет — молча пропускает (лог `warning`).

---

### Task 1: Журнал сборок (`luna_core.assemblies`)

**Files:**
- Create: `packages/luna-core/src/luna_core/assemblies.py`
- Modify: `packages/luna-core/src/luna_core/__init__.py` (экспорт `AssemblyLog`, `RunRecord`; добавить в `__all__`)
- Test: `packages/luna-core/tests/test_assemblies.py`
- Modify: `packages/luna-core/tests/test_public_api.py` (имена в ожидаемом списке экспорта, если тест сверяет `__all__`)

**Interfaces:**
- Produces:
  - `TASK_HEAD = 200`
  - `RunRecord` (frozen dataclass): `id: int, ts: float, workdir: str, parent_session: str, child_session: str, base_profile: str, dynamic: bool, spec: dict, task_head: str, ok: bool, cancelled: bool, hit_max_steps: bool, steps: int, cost: float, mutating: tuple[str, ...] = (), profile_name: str | None = None`; свойство `success -> bool`.
  - `ProfileVersion` (frozen dataclass): `id: int, ts: float, name: str, path: str, action: str, version: int, spec: dict | None, prev: dict | None, note: str, undone: bool`.
  - `AssemblyLog(path: str | Path)` с методами:
    - `record(*, workdir, parent_session, child_session, base_profile, dynamic, spec: dict, task: str, ok, cancelled, hit_max_steps, steps, cost, mutating=(), now: float | None = None) -> RunRecord`
    - `get(run_id: int) -> RunRecord | None`
    - `pending(limit: int = 200) -> list[RunRecord]` — без `profile_name`, самые новые `limit`, в порядке возрастания `id`
    - `last_id() -> int` (0, если пусто); `count_after(run_id: int) -> int`
    - `mark(ids: Iterable[int], profile_name: str) -> None`
    - `recent_for(profile_name: str, limit: int = 10) -> list[RunRecord]` — по `base_profile`, новые первыми
    - `add_version(*, name, path, action, version, spec, prev, note="", now=None) -> ProfileVersion`
    - `versions(name: str | None = None, limit: int = 50) -> list[ProfileVersion]` — новые первыми
    - `set_undone(version_id: int) -> None`
    - `push_notice(level: str, text: str) -> None`; `drain_notices() -> list[tuple[str, str]]` (атомарно читает и удаляет, старые первыми)
    - `get_meta(key: str) -> str | None`; `set_meta(key: str, value: str) -> None`
    - `claim_meta(key: str, *, expect: str | None, value: str) -> bool` — compare-and-set в одной транзакции `BEGIN IMMEDIATE` (нужно Task 4 для «один анализ за раз» между процессами)
    - `close() -> None`

- [ ] **Step 1: Write the failing tests**

```python
"""Assembly journal: runs, profile versions, notices, meta."""

import threading

import pytest
from luna_core.assemblies import TASK_HEAD, AssemblyLog


@pytest.fixture
def journal(tmp_path):
    j = AssemblyLog(tmp_path / "db" / "s.db")
    yield j
    j.close()


def _rec(journal, **kw):
    base = dict(
        workdir="/w",
        parent_session="p1",
        child_session="c1",
        base_profile="general-purpose",
        dynamic=True,
        spec={"name": "general-purpose", "prompt": "Write migrations", "tools": ["read_file"]},
        task="add a column",
        ok=True,
        cancelled=False,
        hit_max_steps=False,
        steps=3,
        cost=0.01,
    )
    base.update(kw)
    return journal.record(**base)


def test_record_roundtrip_and_task_head(journal):
    rec = _rec(journal, task="x" * 500, mutating=("mcp__db__write",))
    got = journal.get(rec.id)
    assert got == rec
    assert len(got.task_head) == TASK_HEAD
    assert got.mutating == ("mcp__db__write",)
    assert got.spec["prompt"] == "Write migrations"
    assert got.profile_name is None


@pytest.mark.parametrize(
    ("ok", "cancelled", "hit", "success"),
    [(True, False, False, True), (False, False, False, False),
     (True, True, False, False), (True, False, True, False)],
)
def test_success_rule(journal, ok, cancelled, hit, success):
    assert _rec(journal, ok=ok, cancelled=cancelled, hit_max_steps=hit).success is success


def test_pending_newest_limit_in_id_order_and_mark(journal):
    ids = [_rec(journal).id for _ in range(5)]
    assert [r.id for r in journal.pending(limit=3)] == ids[2:]
    journal.mark(ids[3:], "migration-writer")
    assert [r.id for r in journal.pending()] == ids[:3]
    assert journal.get(ids[4]).profile_name == "migration-writer"
    assert journal.last_id() == ids[-1]
    assert journal.count_after(ids[1]) == 3


def test_recent_for_profile_newest_first(journal):
    a = _rec(journal, base_profile="x").id
    _rec(journal, base_profile="y")
    b = _rec(journal, base_profile="x").id
    assert [r.id for r in journal.recent_for("x")] == [b, a]


def test_versions_and_undone(journal):
    v1 = journal.add_version(name="m", path="/u.toml", action="save", version=1,
                             spec={"prompt": "a"}, prev=None)
    v2 = journal.add_version(name="m", path="/u.toml", action="update", version=2,
                             spec={"prompt": "b"}, prev={"prompt": "a"})
    journal.add_version(name="other", path="/u.toml", action="save", version=1,
                        spec={}, prev=None)
    assert [v.id for v in journal.versions("m")] == [v2.id, v1.id]
    journal.set_undone(v2.id)
    assert journal.versions("m")[0].undone is True
    assert len(journal.versions()) == 3


def test_notices_drain_once(journal):
    journal.push_notice("info", "one")
    journal.push_notice("warn", "two")
    assert journal.drain_notices() == [("info", "one"), ("warn", "two")]
    assert journal.drain_notices() == []


def test_meta_and_claim(journal):
    assert journal.get_meta("k") is None
    assert journal.claim_meta("k", expect=None, value="1") is True
    assert journal.claim_meta("k", expect=None, value="2") is False
    assert journal.claim_meta("k", expect="1", value="2") is True
    assert journal.get_meta("k") == "2"


def test_claim_is_atomic_across_connections(tmp_path):
    path = tmp_path / "s.db"
    logs = [AssemblyLog(path) for _ in range(4)]
    wins = []

    def go(j):
        wins.append(j.claim_meta("lock", expect=None, value="x"))

    threads = [threading.Thread(target=go, args=(j,)) for j in logs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1
    for j in logs:
        j.close()


def test_shares_file_with_event_log(tmp_path):
    from luna_core.session import EventLog

    path = tmp_path / "s.db"
    log = EventLog(path)
    j = AssemblyLog(path)
    sid = log.create("/w")
    _rec(j)
    assert log.info(sid) is not None and j.last_id() == 1
    j.close()
    log.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_assemblies.py -q -o addopts=""`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna_core.assemblies'`

- [ ] **Step 3: Implement `assemblies.py`**

```python
"""The assembly journal: one row per subagent run, auto-profile versions, notices.

Lives in the same SQLite file as the session event log (``sessions.db``) but
uses its own connection, so the runner can write to it without touching the
event log's lock. Only what the *parent* wrote is stored: the assembly
(prompt, tools, skills, model, context) and the head of the task. Tool
output and the subagent's answer never enter the journal.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: How much of the task text the journal keeps.
TASK_HEAD = 200

_DDL = """
CREATE TABLE IF NOT EXISTS subagent_runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    workdir TEXT NOT NULL,
    parent_session TEXT NOT NULL,
    child_session TEXT NOT NULL,
    base_profile TEXT NOT NULL,
    dynamic INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    task_head TEXT NOT NULL,
    ok INTEGER NOT NULL,
    cancelled INTEGER NOT NULL,
    hit_max_steps INTEGER NOT NULL,
    steps INTEGER NOT NULL,
    cost REAL NOT NULL,
    mutating_json TEXT NOT NULL DEFAULT '[]',
    profile_name TEXT
);
CREATE INDEX IF NOT EXISTS subagent_runs_pending ON subagent_runs(profile_name, id);
CREATE INDEX IF NOT EXISTS subagent_runs_by_profile ON subagent_runs(base_profile, id);
CREATE TABLE IF NOT EXISTS subagent_profile_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    version INTEGER NOT NULL,
    spec_json TEXT,
    prev_json TEXT,
    note TEXT NOT NULL DEFAULT '',
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS luna_notices(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subagent_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_RUN_COLS = (
    "id, ts, workdir, parent_session, child_session, base_profile, dynamic, spec_json, "
    "task_head, ok, cancelled, hit_max_steps, steps, cost, mutating_json, profile_name"
)
_VER_COLS = "id, ts, name, path, action, version, spec_json, prev_json, note, undone"


@dataclass(frozen=True)
class RunRecord:
    """One subagent run as the journal keeps it."""

    id: int
    ts: float
    workdir: str
    parent_session: str
    child_session: str
    base_profile: str
    dynamic: bool
    spec: dict
    task_head: str
    ok: bool
    cancelled: bool
    hit_max_steps: bool
    steps: int
    cost: float
    mutating: tuple[str, ...] = ()
    profile_name: str | None = None

    @property
    def success(self) -> bool:
        """Finished without error, cancel or running out of steps (a rough measure)."""
        return self.ok and not self.cancelled and not self.hit_max_steps


@dataclass(frozen=True)
class ProfileVersion:
    """One saved change of a profile (for history and undo)."""

    id: int
    ts: float
    name: str
    path: str
    action: str
    version: int
    spec: dict | None
    prev: dict | None
    note: str
    undone: bool


def _loads(text: str | None) -> dict | None:
    return None if text is None else json.loads(text)


def _run(row: tuple) -> RunRecord:
    return RunRecord(
        id=row[0],
        ts=row[1],
        workdir=row[2],
        parent_session=row[3],
        child_session=row[4],
        base_profile=row[5],
        dynamic=bool(row[6]),
        spec=json.loads(row[7]),
        task_head=row[8],
        ok=bool(row[9]),
        cancelled=bool(row[10]),
        hit_max_steps=bool(row[11]),
        steps=row[12],
        cost=row[13],
        mutating=tuple(json.loads(row[14])),
        profile_name=row[15],
    )


def _version(row: tuple) -> ProfileVersion:
    return ProfileVersion(
        id=row[0],
        ts=row[1],
        name=row[2],
        path=row[3],
        action=row[4],
        version=row[5],
        spec=_loads(row[6]),
        prev=_loads(row[7]),
        note=row[8],
        undone=bool(row[9]),
    )


class AssemblyLog:
    """The assembly journal over ``path`` (usually ``sessions.db``)."""

    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_DDL)
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def _write(self):
        """Process lock plus one ``BEGIN IMMEDIATE`` transaction (atomic across processes)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _read(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # --- runs -------------------------------------------------------------

    def record(
        self,
        *,
        workdir: str,
        parent_session: str,
        child_session: str,
        base_profile: str,
        dynamic: bool,
        spec: dict,
        task: str,
        ok: bool,
        cancelled: bool,
        hit_max_steps: bool,
        steps: int,
        cost: float,
        mutating: Iterable[str] = (),
        now: float | None = None,
    ) -> RunRecord:
        """Append one run and return it."""
        ts = time.time() if now is None else now
        head = task[:TASK_HEAD]
        muts = list(mutating)
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO subagent_runs(ts, workdir, parent_session, child_session, "
                "base_profile, dynamic, spec_json, task_head, ok, cancelled, hit_max_steps, "
                "steps, cost, mutating_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts,
                    workdir,
                    parent_session,
                    child_session,
                    base_profile,
                    int(dynamic),
                    json.dumps(spec, ensure_ascii=False),
                    head,
                    int(ok),
                    int(cancelled),
                    int(hit_max_steps),
                    steps,
                    cost,
                    json.dumps(muts),
                ),
            )
            run_id = cur.lastrowid
        return RunRecord(
            run_id, ts, workdir, parent_session, child_session, base_profile, dynamic,
            spec, head, ok, cancelled, hit_max_steps, steps, cost, tuple(muts),
        )

    def get(self, run_id: int) -> RunRecord | None:
        """The run ``run_id``, or None."""
        rows = self._read(f"SELECT {_RUN_COLS} FROM subagent_runs WHERE id = ?", (run_id,))
        return _run(rows[0]) if rows else None

    def pending(self, limit: int = 200) -> list[RunRecord]:
        """The newest ``limit`` runs not yet folded into a profile, oldest first."""
        rows = self._read(
            f"SELECT {_RUN_COLS} FROM subagent_runs WHERE profile_name IS NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [_run(r) for r in reversed(rows)]

    def last_id(self) -> int:
        """The newest run id (0 when the journal is empty)."""
        return self._read("SELECT COALESCE(MAX(id), 0) FROM subagent_runs")[0][0]

    def count_after(self, run_id: int) -> int:
        """How many runs were recorded after ``run_id``."""
        return self._read("SELECT COUNT(*) FROM subagent_runs WHERE id > ?", (run_id,))[0][0]

    def mark(self, ids: Iterable[int], profile_name: str) -> None:
        """Mark runs as folded into ``profile_name``."""
        with self._write() as conn:
            conn.executemany(
                "UPDATE subagent_runs SET profile_name = ? WHERE id = ?",
                [(profile_name, i) for i in ids],
            )

    def recent_for(self, profile_name: str, limit: int = 10) -> list[RunRecord]:
        """The newest runs that used profile ``profile_name`` as their base."""
        rows = self._read(
            f"SELECT {_RUN_COLS} FROM subagent_runs WHERE base_profile = ? "
            "ORDER BY id DESC LIMIT ?",
            (profile_name, limit),
        )
        return [_run(r) for r in rows]

    # --- profile versions -------------------------------------------------

    def add_version(
        self,
        *,
        name: str,
        path: str,
        action: str,
        version: int,
        spec: dict | None,
        prev: dict | None,
        note: str = "",
        now: float | None = None,
    ) -> ProfileVersion:
        """Record a change of profile ``name`` in ``path``."""
        ts = time.time() if now is None else now
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO subagent_profile_versions(ts, name, path, action, version, "
                "spec_json, prev_json, note) VALUES (?,?,?,?,?,?,?,?)",
                (
                    ts,
                    name,
                    path,
                    action,
                    version,
                    None if spec is None else json.dumps(spec, ensure_ascii=False),
                    None if prev is None else json.dumps(prev, ensure_ascii=False),
                    note,
                ),
            )
            vid = cur.lastrowid
        return ProfileVersion(vid, ts, name, path, action, version, spec, prev, note, False)

    def versions(self, name: str | None = None, limit: int = 50) -> list[ProfileVersion]:
        """Profile changes, newest first (all profiles when ``name`` is None)."""
        if name is None:
            rows = self._read(
                f"SELECT {_VER_COLS} FROM subagent_profile_versions ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self._read(
                f"SELECT {_VER_COLS} FROM subagent_profile_versions WHERE name = ? "
                "ORDER BY id DESC LIMIT ?",
                (name, limit),
            )
        return [_version(r) for r in rows]

    def set_undone(self, version_id: int) -> None:
        """Mark a change as rolled back."""
        with self._write() as conn:
            conn.execute(
                "UPDATE subagent_profile_versions SET undone = 1 WHERE id = ?", (version_id,)
            )

    # --- notices and meta -------------------------------------------------

    def push_notice(self, level: str, text: str) -> None:
        """Queue a notice for the next finished turn (any session of this user)."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO luna_notices(ts, level, text) VALUES (?,?,?)",
                (time.time(), level, text),
            )

    def drain_notices(self) -> list[tuple[str, str]]:
        """Take every queued notice (oldest first); each is returned once."""
        with self._write() as conn:
            rows = conn.execute("SELECT id, level, text FROM luna_notices ORDER BY id").fetchall()
            if rows:
                conn.execute("DELETE FROM luna_notices WHERE id <= ?", (rows[-1][0],))
        return [(level, text) for _id, level, text in rows]

    def get_meta(self, key: str) -> str | None:
        """A stored value, or None."""
        rows = self._read("SELECT value FROM subagent_meta WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        """Store ``value`` under ``key``."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO subagent_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def claim_meta(self, key: str, *, expect: str | None, value: str) -> bool:
        """Set ``key`` to ``value`` only if it currently equals ``expect`` (None = absent)."""
        with self._write() as conn:
            row = conn.execute("SELECT value FROM subagent_meta WHERE key = ?", (key,)).fetchone()
            if (row[0] if row else None) != expect:
                return False
            conn.execute(
                "INSERT INTO subagent_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            return True

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()


__all__ = ["TASK_HEAD", "AssemblyLog", "ProfileVersion", "RunRecord"]
```

- [ ] **Step 4: Export from the package**

В `packages/luna-core/src/luna_core/__init__.py` добавить `from luna_core.assemblies import AssemblyLog, RunRecord` и оба имени в `__all__` (в том же стиле, что `DynamicSettings`). Если `test_public_api.py` сверяет полный список — добавить туда.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/luna-core/tests/test_assemblies.py packages/luna-core/tests/test_public_api.py -q -o addopts=""`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add packages/luna-core/src/luna_core/assemblies.py packages/luna-core/src/luna_core/__init__.py packages/luna-core/tests/test_assemblies.py packages/luna-core/tests/test_public_api.py
git commit -m "feat(core): assembly journal for subagent runs"
```

---

### Task 2: Раннер пишет каждый запуск в журнал

**Files:**
- Modify: `packages/luna-core/src/luna_core/subagents.py` (`SubagentRunner`: поля и метод `run`)
- Test: `packages/luna-core/tests/test_subagents.py` (новые тесты в конце файла; фикстуры `log`, `parent_agent`, `init_repo` уже есть)

**Interfaces:**
- Consumes: `AssemblyLog.record(...)`, `RunRecord` (Task 1); существующие `_mutating(parent, name)`, `WRITE_TOOLS`, `SubagentResult.spec/.dynamic/.session_id/.steps/.cost/.hit_max_steps/.cancelled`.
- Produces:
  - `SubagentRunner.journal: AssemblyLog | None = None`
  - `SubagentRunner.on_recorded: Callable[[RunRecord, Session], None] | None = None`
  - Запись делается в `SubagentRunner.run` после `_run`, только когда `result.session_id` непустой (дочерняя сессия реально создавалась). `base_profile = result.spec.name`, `dynamic = result.dynamic`, `spec = result.spec.to_dict()`, `workdir = session.workdir`, `parent_session = session.id`, `mutating = [t for t in result.spec.tools if t in WRITE_TOOLS or _mutating(parent, t)]`.
  - Ошибка записи (`sqlite3.Error`, `OSError`) и любое исключение из `on_recorded` логируются через `logging.getLogger(__name__).warning(...)` и **не** ломают запуск.

- [ ] **Step 1: Write the failing tests**

Добавить в конец `packages/luna-core/tests/test_subagents.py` (импорт `from luna_core.assemblies import AssemblyLog` — в шапку файла):

```python
async def test_runs_are_journaled_without_tool_output(tmp_path, log):
    journal = AssemblyLog(tmp_path / "db" / "s.db")
    child = FakeProvider(
        [
            FakeProvider.call("read_file", {"path": "secret.txt"}),
            FakeProvider.text("the answer is 42"),
        ]
    )
    (tmp_path / "secret.txt").write_text("TOP-SECRET-CONTENT")
    seen = []
    runner = SubagentRunner(
        {},
        model_for=lambda s: (child, "m"),
        journal=journal,
        on_recorded=lambda rec, sess: seen.append((rec, sess.id)),
    )
    parent = parent_agent(tmp_path, [], child)
    session = Session.create(log, str(tmp_path), undo=False)
    spec = SubagentSpec("scout", "d", "Find facts", ("read_file",), isolation="none")
    result = await runner.run(parent, session, spec, "look " * 100, dynamic=True)
    assert result.ok
    (rec,) = journal.pending()
    assert rec.child_session == result.session_id
    assert rec.parent_session == session.id
    assert rec.workdir == str(tmp_path)
    assert rec.base_profile == "scout" and rec.dynamic is True
    assert rec.spec["prompt"] == "Find facts" and rec.spec["tools"] == ["read_file"]
    assert len(rec.task_head) == 200
    assert rec.success and rec.steps == result.steps
    raw = repr(rec)
    assert "TOP-SECRET-CONTENT" not in raw and "42" not in raw
    assert seen == [(rec, session.id)]
    journal.close()


async def test_mutating_tools_are_journaled(tmp_path, log):
    journal = AssemblyLog(tmp_path / "db" / "s.db")
    child = FakeProvider([FakeProvider.text("done")])
    runner = SubagentRunner({}, model_for=lambda s: (child, "m"), journal=journal)
    parent = parent_agent(tmp_path, [], child, tools=[*builtin_tools(), mcp__db__write])
    session = Session.create(log, str(tmp_path), undo=False)
    spec = SubagentSpec(
        "db", "d", "p", ("read_file", "mcp__db__write"), unsafe=True, isolation="none"
    )
    await runner.run(parent, session, spec, "x")
    assert journal.pending()[0].mutating == ("mcp__db__write",)
    journal.close()


async def test_refused_run_is_not_journaled(tmp_path, log):
    journal = AssemblyLog(tmp_path / "db" / "s.db")
    child = FakeProvider([FakeProvider.text("done")])
    runner = SubagentRunner({}, model_for=lambda s: (child, "m"), journal=journal)
    parent = parent_agent(tmp_path, [], child)
    session = Session.create(log, str(tmp_path), undo=False)
    result = await runner.run(parent, session, "no-such-profile", "x")
    assert not result.ok and journal.pending() == []
    journal.close()


async def test_journal_failure_does_not_break_the_run(tmp_path, log):
    journal = AssemblyLog(tmp_path / "db" / "s.db")
    journal.close()  # every write now raises sqlite3.ProgrammingError (a sqlite3.Error)
    child = FakeProvider([FakeProvider.text("done")])

    def boom(rec, sess):
        raise RuntimeError("callback bug")

    runner = SubagentRunner(
        {}, model_for=lambda s: (child, "m"), journal=journal, on_recorded=boom
    )
    parent = parent_agent(tmp_path, [], child)
    session = Session.create(log, str(tmp_path), undo=False)
    spec = SubagentSpec("s", "d", "p", ("ls",), isolation="none")
    result = await runner.run(parent, session, spec, "x")
    assert result.ok and result.text == "done"


async def test_callback_failure_does_not_break_the_run(tmp_path, log):
    journal = AssemblyLog(tmp_path / "db" / "s.db")
    child = FakeProvider([FakeProvider.text("done")])

    def boom(rec, sess):
        raise RuntimeError("callback bug")

    runner = SubagentRunner(
        {}, model_for=lambda s: (child, "m"), journal=journal, on_recorded=boom
    )
    parent = parent_agent(tmp_path, [], child)
    session = Session.create(log, str(tmp_path), undo=False)
    result = await runner.run(parent, session, SubagentSpec("s", "d", "p", ("ls",)), "x")
    assert result.ok and len(journal.pending()) == 1
    journal.close()
```

`mcp__db__write` — уже существующий в файле тестовый мутирующий инструмент (используется в `test_profile_mutating_tool_needs_unsafe`). Если `"no-such-profile"` в этом раннере возвращает ошибку иначе (исключение), подстрой тест под фактический путь ошибки, но утверждение «журнал пуст» сохрани.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_subagents.py -q -o addopts="" -k "journal or journaled"`
Expected: FAIL — `TypeError: SubagentRunner.__init__() got an unexpected keyword argument 'journal'`

- [ ] **Step 3: Implement**

В `subagents.py`:

```python
import logging
import sqlite3

from luna_core.assemblies import AssemblyLog, RunRecord

_log = logging.getLogger(__name__)
```

(если `logging`/`_log` уже есть — переиспользовать; импорт `AssemblyLog`/`RunRecord` под `TYPE_CHECKING` не нужен — модуль `assemblies` не импортирует `subagents`, цикла нет).

Поля `SubagentRunner` (после `skill_names`):

```python
    journal: AssemblyLog | None = None
    on_recorded: Callable[[RunRecord, Session], None] | None = None
```

В `run()` заменить `return await self._run(...)` на:

```python
            result = await self._run(
                parent, session, spec, task, isolation, emit, cancel, turn_id,
                dynamic, resume, reserved,
            )
            self._journal(parent, session, task, result)
            return result
```

Новый метод:

```python
    def _journal(self, parent, session: Session, task: str, result: SubagentResult) -> None:
        """Record a run that actually started; never let the journal break the run."""
        if self.journal is None or not result.session_id or result.spec is None:
            return
        spec = result.spec
        try:
            record = self.journal.record(
                workdir=session.workdir,
                parent_session=session.id,
                child_session=result.session_id,
                base_profile=spec.name,
                dynamic=result.dynamic,
                spec=spec.to_dict(),
                task=task,
                ok=result.ok,
                cancelled=result.cancelled,
                hit_max_steps=result.hit_max_steps,
                steps=result.steps,
                cost=result.cost,
                mutating=[t for t in spec.tools if t in WRITE_TOOLS or _mutating(parent, t)],
            )
        except (sqlite3.Error, OSError) as exc:
            _log.warning("subagent journal write failed: %s", exc)
            return
        if self.on_recorded is not None:
            try:
                self.on_recorded(record, session)
            except Exception:  # noqa: BLE001 - a journal hook must never fail a run
                _log.exception("on_recorded failed")
```

Проверь атрибут рабочей папки сессии (`session.workdir`) — если в `Session` он называется иначе, используй фактическое имя.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/luna-core/tests -q -o addopts=""`
Expected: PASS (весь набор ядра, чтобы поймать регрессии раннера)

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/subagents.py packages/luna-core/tests/test_subagents.py
git commit -m "feat(subagents): record every started run in the assembly journal"
```

---

### Task 3: Анализ журнала (`luna_core.distill`)

**Files:**
- Create: `packages/luna-core/src/luna_core/distill.py`
- Test: `packages/luna-core/tests/test_distill.py`

**Interfaces:**
- Consumes: `RunRecord` (Task 1), `SubagentSpec`, `CHILD_FORBIDDEN`, `WRITE_TOOLS` из `luna_core.subagents`; `ModelRequest`, `Provider`, `StreamAccumulator` из `luna_core.providers.base`; `Message` из `luna_core.messages`; `ProviderError`, `LunaCoreError` из `luna_core.errors`.
- Produces:
  - константы `MIN_RUNS = 5`, `MIN_SUCCESS = 0.7`, `DISABLE_WINDOW = 10`, `DISABLE_MIN_RUNS = 5`, `DISABLE_BELOW = 0.5`, `MAX_OUTPUT_TOKENS = 4000`, `FIELD_CHARS = 1500`, `INPUT_CHARS = 60_000`
  - `DISTILL_PROMPT: str`
  - `class DistillError(LunaCoreError)`
  - `Candidate` (frozen): `name, description, prompt: str, tools: tuple[str, ...], skills: tuple[str, ...], model: str | None, context: str, max_steps: int, runs: tuple[int, ...]`; метод `to_spec(*, unsafe: bool, version: int) -> SubagentSpec` (с `auto=True`, `isolation="auto"`)
  - `Verdict` (frozen): `candidate: Candidate, ok: bool, reason: str, runs: tuple[int, ...], successes: int, workdirs: tuple[str, ...], writes: bool`
  - `render_journal(records: Sequence[RunRecord]) -> str`
  - `parse_candidates(text: str) -> list[Candidate]`
  - `check_candidate(candidate, runs_by_id: Mapping[int, RunRecord], *, taken: Mapping[str, bool], allow_write: bool) -> Verdict` — `taken`: имя → `True`, если это auto-профиль (его можно обновить), `False` — ручной/встроенный (нельзя)
  - `async propose(provider: Provider, model: str, records: Sequence[RunRecord]) -> list[Candidate]`
  - `should_disable(recent: Sequence[RunRecord]) -> bool` — `recent` новые первыми

- [ ] **Step 1: Write the failing tests**

```python
"""Distill: journal → model → candidate profiles, checked by code."""

import json

import pytest
from luna_core.assemblies import RunRecord
from luna_core.distill import (
    INPUT_CHARS,
    Candidate,
    DistillError,
    check_candidate,
    parse_candidates,
    propose,
    render_journal,
    should_disable,
)
from luna_core.errors import ProviderError
from luna_core.providers.fake import FakeProvider


def rec(i, *, ok=True, workdir="/w", tools=("read_file", "write_file"), mutating=(),
        skills=(), prompt="Write a SQL migration", task="add column"):
    return RunRecord(
        id=i, ts=float(i), workdir=workdir, parent_session="p", child_session=f"c{i}",
        base_profile="general-purpose", dynamic=True,
        spec={"name": "general-purpose", "prompt": prompt, "tools": list(tools),
              "skills": list(skills), "model": "fast", "context": ""},
        task_head=task, ok=ok, cancelled=False, hit_max_steps=False, steps=3, cost=0.0,
        mutating=tuple(mutating),
    )


def cand(runs, *, name="migration-writer", tools=("read_file",), skills=()):
    return Candidate(name, "Writes migrations", "You write SQL migrations.", tuple(tools),
                     tuple(skills), "fast", "", 30, tuple(runs))


def by_id(records):
    return {r.id: r for r in records}


PAYLOAD = {
    "profiles": [
        {"name": "migration-writer", "description": "Writes migrations",
         "prompt": "You write SQL migrations.", "tools": ["read_file"], "skills": [],
         "model": "fast", "context": "", "max_steps": 20, "runs": [1, 2, 3]}
    ]
}


def test_parse_plain_fenced_and_prose():
    plain = json.dumps(PAYLOAD)
    fenced = f"Here you go:\n```json\n{plain}\n```\nDone."
    for text in (plain, fenced):
        (c,) = parse_candidates(text)
        assert c.name == "migration-writer" and c.tools == ("read_file",)
        assert c.runs == (1, 2, 3) and c.max_steps == 20


@pytest.mark.parametrize(
    "text",
    ["", "no json here", "[1, 2]", '{"profiles": "nope"}', "{broken",
     '{"profiles": [{"name": 5}]}'],
)
def test_parse_garbage_gives_nothing(text):
    assert parse_candidates(text) == []


def test_parse_skips_bad_items_keeps_good():
    bad = dict(PAYLOAD["profiles"][0], name="Bad Name!")
    no_prompt = dict(PAYLOAD["profiles"][0], name="x-ok", prompt="")
    wrong_tools = dict(PAYLOAD["profiles"][0], name="y-ok", tools="read_file")
    wrong_runs = dict(PAYLOAD["profiles"][0], name="z-ok", runs=["1"])
    huge_steps = dict(PAYLOAD["profiles"][0], name="w-ok", max_steps=10_000)
    text = json.dumps({"profiles": [bad, no_prompt, wrong_tools, wrong_runs, huge_steps,
                                    PAYLOAD["profiles"][0]]})
    assert [c.name for c in parse_candidates(text)] == ["migration-writer"]


def test_thresholds_five_runs_seventy_percent():
    runs = [rec(i) for i in range(1, 6)]  # 5/5
    assert check_candidate(cand(range(1, 6)), by_id(runs), taken={}, allow_write=False).ok
    four = check_candidate(cand(range(1, 5)), by_id(runs), taken={}, allow_write=False)
    assert not four.ok and "5" in four.reason
    mixed = [rec(i, ok=i <= 7) for i in range(1, 11)]  # 7/10 = 70 %
    assert check_candidate(cand(range(1, 11)), by_id(mixed), taken={}, allow_write=False).ok
    worse = [rec(i, ok=i <= 6) for i in range(1, 11)]  # 6/10
    v = check_candidate(cand(range(1, 11)), by_id(worse), taken={}, allow_write=False)
    assert not v.ok and "70" in v.reason


def test_invented_run_ids_do_not_count():
    runs = [rec(i) for i in range(1, 5)]
    v = check_candidate(cand([1, 2, 3, 4, 99, 100]), by_id(runs), taken={}, allow_write=False)
    assert not v.ok and v.runs == (1, 2, 3, 4)


def test_tools_must_come_from_the_runs():
    runs = [rec(i, tools=("read_file",)) for i in range(1, 6)]
    v = check_candidate(cand(range(1, 6), tools=("read_file", "mcp__x__y")), by_id(runs),
                        taken={}, allow_write=True)
    assert not v.ok and "mcp__x__y" in v.reason


def test_forbidden_tool_rejected():
    runs = [rec(i, tools=("read_file", "task")) for i in range(1, 6)]
    v = check_candidate(cand(range(1, 6), tools=("task",)), by_id(runs), taken={},
                        allow_write=True)
    assert not v.ok


def test_skills_must_come_from_the_runs():
    runs = [rec(i, skills=("sql",)) for i in range(1, 6)]
    ok = check_candidate(cand(range(1, 6), skills=("sql",)), by_id(runs), taken={},
                         allow_write=False)
    bad = check_candidate(cand(range(1, 6), skills=("pdf",)), by_id(runs), taken={},
                          allow_write=False)
    assert ok.ok and not bad.ok


def test_manual_name_is_protected_auto_name_may_update():
    runs = [rec(i) for i in range(1, 6)]
    manual = check_candidate(cand(range(1, 6)), by_id(runs),
                             taken={"migration-writer": False}, allow_write=False)
    auto = check_candidate(cand(range(1, 6)), by_id(runs),
                           taken={"migration-writer": True}, allow_write=False)
    assert not manual.ok and "hand-written" in manual.reason
    assert auto.ok


def test_write_ceiling():
    runs = [rec(i, tools=("read_file", "write_file", "mcp__db__q"),
                mutating=("mcp__db__q",)) for i in range(1, 6)]
    for tools in (("write_file",), ("mcp__db__q",)):
        denied = check_candidate(cand(range(1, 6), tools=tools), by_id(runs), taken={},
                                 allow_write=False)
        allowed = check_candidate(cand(range(1, 6), tools=tools), by_id(runs), taken={},
                                  allow_write=True)
        assert not denied.ok and "allow_write" in denied.reason
        assert allowed.ok and allowed.writes


def test_workdirs_reported():
    runs = [rec(i, workdir="/a" if i % 2 else "/b") for i in range(1, 6)]
    v = check_candidate(cand(range(1, 6)), by_id(runs), taken={}, allow_write=False)
    assert v.workdirs == ("/a", "/b") and v.successes == 5


def test_to_spec_is_auto():
    spec = cand([1]).to_spec(unsafe=False, version=3)
    assert spec.auto and spec.version == 3 and spec.isolation == "auto"
    assert spec.tools == ("read_file",) and spec.model == "fast"


def test_render_journal_has_no_answers_and_is_capped():
    text = render_journal([rec(i, prompt="P" * 5000) for i in range(1, 300)])
    assert len(text) <= INPUT_CHARS
    first = json.loads(text.splitlines()[0])
    assert set(first) == {"id", "workdir", "base", "dynamic", "success", "task", "prompt",
                          "tools", "skills", "model", "context"}
    # the newest records survive the cap
    assert json.loads(text.splitlines()[-1])["id"] == 299


async def test_propose_one_request():
    provider = FakeProvider([FakeProvider.text(json.dumps(PAYLOAD))])
    out = await propose(provider, "fast-model", [rec(1), rec(2)])
    assert [c.name for c in out] == ["migration-writer"]
    (request,) = provider.requests
    assert request.model == "fast-model" and request.max_tokens == 4000


async def test_propose_empty_journal_sends_nothing():
    provider = FakeProvider([])
    assert await propose(provider, "m", []) == []
    assert provider.requests == []


async def test_propose_provider_error_is_distill_error():
    class Broken:
        name = "broken"

        async def stream(self, request):
            raise ProviderError("429 slow down")
            yield  # pragma: no cover

    with pytest.raises(DistillError, match="429"):
        await propose(Broken(), "m", [rec(1)])


def test_should_disable():
    assert not should_disable([rec(i, ok=False) for i in range(4)])  # < 5 runs: too early
    assert should_disable([rec(i, ok=i < 2) for i in range(5)])  # 2/5
    assert not should_disable([rec(i, ok=i < 5) for i in range(10)])  # 5/10 = 50 %
    assert should_disable([rec(i, ok=i < 4) for i in range(10)])  # 4/10
    # only the newest 10 count
    newest_good = [rec(i) for i in range(10)] + [rec(i, ok=False) for i in range(10, 30)]
    assert not should_disable(newest_good)
```

Если `ProviderError` в ядре требует других аргументов конструктора — подстрой вызов в `Broken`, не меняя смысла теста.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_distill.py -q -o addopts=""`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna_core.distill'`

- [ ] **Step 3: Implement `distill.py`**

```python
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
        """The profile to save (always ``auto``)."""
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
        return verdict(
            f"{successes}/{len(runs)} succeeded (needs {int(MIN_SUCCESS * 100)} %)"
        )
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
    """True when an auto profile's newest runs mostly fail (``recent``: newest first)."""
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
```

Проверка «модель — строка или null» и `int(MIN_SUCCESS * 100)` = 70 — тест `"70" in v.reason` на это опирается.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/luna-core/tests/test_distill.py -q -o addopts=""`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/distill.py packages/luna-core/tests/test_distill.py
git commit -m "feat(core): distill — candidate profiles from the journal, checked by code"
```

---

### Task 4: Автопрофили в приложении (`luna/extensions/autoprofiles.py`)

**Files:**
- Create: `luna/extensions/autoprofiles.py`
- Test: `tests/test_autoprofiles.py`

**Interfaces:**
- Consumes: `AssemblyLog`, `RunRecord`, `ProfileVersion` (Task 1); `propose`, `check_candidate`, `should_disable`, `DistillError`, `Verdict` (Task 3); `BUILTIN_SUBAGENTS`, `SubagentSpec` из `luna_core.subagents`; `config_dir` из `luna.config.config`; `db_path` из `luna.core.store`.
- Produces:
  - `HOUR = 3600`, `WEEK = 7 * 86400`, `EVERY = 20`
  - `open_journal(env=None) -> AssemblyLog` — один экземпляр на путь `sessions.db` в процессе (кэш как `store._LOGS`); `close_journals()` для тестов
  - `@dataclass DistillReport`: `saved: list[str]` (тексты уведомлений), `rejected: list[tuple[str, str]]` (имя, причина), `skipped: str = ""`; метод `lines() -> list[str]`
  - `class AutoProfiles(journal: AssemblyLog, *, env=None, clock=time.time)`:
    - `user_file() -> Path`; `project_file(workdir: str) -> Path` (`<workdir>/.luna/subagents.toml`)
    - `due_by_count() -> bool` — `journal.count_after(int(meta distill_last_id or 0)) >= EVERY`
    - `due_weekly() -> bool` — есть `pending()` и `now - distill_last_ts >= WEEK` (нет метки → True)
    - `wait_left() -> float` — секунд до конца часового окна (0, если можно)
    - `async distill(provider, model, *, allow_write: bool) -> DistillReport`
    - `check_disable(name: str) -> str | None`
    - `set_disabled(name: str, disabled: bool, *, workdir: str) -> str`
    - `undo(name: str) -> str`
    - `history(name: str | None = None) -> list[str]`
  - `deliver_notices(journal: AssemblyLog, emit: Callable[[str, str], None]) -> int` — drain и вызвать `emit(level, text)` на каждое; возвращает число

Поведение `distill` (по порядку):
1. Часовой лимит атомарно: `prev = journal.get_meta("distill_last_ts")`; если `prev` есть и `now - float(prev) < HOUR` → `DistillReport(skipped=f"analysis ran recently; next in {ceil(left/60)} min")`. Иначе `journal.claim_meta("distill_last_ts", expect=prev, value=str(now))`; проиграл гонку → тот же `skipped`. Так из двух одновременных вызовов (даже в разных процессах) запрос делает один.
2. `records = journal.pending(200)`; `journal.set_meta("distill_last_id", str(journal.last_id()))`. Пусто → `skipped="nothing new in the journal"` (запроса нет).
3. `candidates = await propose(provider, model, records)`; `DistillError` → `skipped=f"analysis failed: {exc}"`.
4. Для каждого кандидата: сначала `check_candidate` с `taken={}` (чтобы узнать `workdirs`), затем путь: `user_file()` если `len(workdirs) >= 2`, иначе `project_file(workdirs[0])`; `taken = self._taken(path)` (имена из файла: `True` если `auto = true`, иначе `False`; плюс все `BUILTIN_SUBAGENTS` как `False`); повторный `check_candidate` с этим `taken`. Не прошёл → `rejected.append((name, reason))`.
5. Прошёл → `self._save(verdict, path, unsafe=verdict.writes)`: чтение `tomlkit` (ошибка чтения/разбора → `rejected` с причиной `cannot read <path>: …`, файл не трогаем), `prev` = текущая таблица как `dict` или `None`, `version = prev.get("version", 1) + 1` если `prev` иначе `1`, запись таблицы атомарно (tmp + `os.replace`), `journal.add_version(action="update" if prev else "save", …)`, `journal.mark(verdict.runs, name)`, `journal.push_notice("info", f"Luna сохранила профиль `{name}` ({successes}/{len(runs)} успешно)")`, текст — в `report.saved`.

Таблица профиля в TOML: `description`, `prompt`, `tools`, `skills` (если не пусто), `model` (если есть), `context` (если не пусто), `max_steps`, `unsafe = true` (только если пишет), `auto = true`, `version`. Без `isolation` (по умолчанию `auto`).

`check_disable(name)`: найти последнюю версию `name` (`journal.versions(name, 1)`); нет → `None`. Прочитать таблицу из её `path`; не auto или уже `disabled` → `None`. `should_disable(journal.recent_for(name))` → записать `disabled = true`, `add_version(action="disable", note="auto: low success")`, `push_notice("warn", f"Luna отключила профиль `{name}`: из последних {n} запусков успешны {k}")`, вернуть текст.

`set_disabled(name, disabled, *, workdir)`: искать таблицу в `project_file(workdir)`, затем `user_file()`; не нашлась → `f"profile {name!r} is not defined in a subagents.toml (built-in profiles cannot be disabled here)"`. Записать флаг (при `enable` — удалить ключ `disabled`), `add_version(action="disable"|"enable")`, вернуть текст подтверждения.

`undo(name)`: последняя версия `name` с `undone = False`; нет → `"nothing to undo for <name>"`. Текущая таблица в `path` должна равняться `version.spec` (сравнение `dict`), иначе `"<name> was edited by hand after that change; nothing was undone"`. Иначе восстановить `version.prev` (или удалить таблицу, если `prev is None`), `set_undone(version.id)`, `add_version(action="undo", spec=version.prev, prev=version.spec, version=...)` и вернуть `"<name>: restored version N"` / `"<name>: removed"`. Запись `action="undo"` сама по себе не откатывается (повторный `undo` идёт к предыдущей неоткаченной версии): при поиске пропускать строки с `action == "undo"`.

`history(name)`: строки вида `2026-11-17 14:02  save    migration-writer v1  ~/.config/luna/subagents.toml` (+ ` (undone)`), новые первыми.

- [ ] **Step 1: Write the failing tests**

```python
"""Auto-profiles: distill flow, files, versions, undo, disable, hour gate."""

import asyncio
import json
import tomllib

import pytest
from luna_core.assemblies import AssemblyLog
from luna_core.providers.fake import FakeProvider
from luna_core.subagents import load_subagents

from luna.extensions.autoprofiles import EVERY, HOUR, AutoProfiles, deliver_notices


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def journal(tmp_path):
    j = AssemblyLog(tmp_path / "s.db")
    yield j
    j.close()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def auto(journal, clock, isolated_config_home):
    return AutoProfiles(journal, clock=clock)


def add_runs(journal, n, *, workdir, ok=lambda i: True, tools=("read_file",), mutating=()):
    return [
        journal.record(
            workdir=str(workdir), parent_session="p", child_session=f"c{i}",
            base_profile="general-purpose", dynamic=True,
            spec={"name": "general-purpose", "prompt": "Write a SQL migration",
                  "tools": list(tools), "skills": [], "model": None, "context": ""},
            task="add column", ok=ok(i), cancelled=False, hit_max_steps=False, steps=2,
            cost=0.0, mutating=mutating,
        ).id
        for i in range(n)
    ]


def reply(ids, *, name="migration-writer", tools=("read_file",)):
    return FakeProvider.text(json.dumps({"profiles": [{
        "name": name, "description": "Writes migrations",
        "prompt": "You write SQL migrations.", "tools": list(tools), "skills": [],
        "model": None, "context": "", "max_steps": 20, "runs": list(ids)}]}))


def table(path, name):
    return tomllib.loads(path.read_text())["subagent"][name]


async def test_saves_to_project_file_and_notifies(auto, journal, tmp_path):
    proj = tmp_path / "proj"
    ids = add_runs(journal, 6, workdir=proj, ok=lambda i: i != 0)
    provider = FakeProvider([reply(ids)])
    report = await auto.distill(provider, "fast", allow_write=False)
    assert report.saved == ["Luna сохранила профиль `migration-writer` (5/6 успешно)"]
    saved = table(proj / ".luna" / "subagents.toml", "migration-writer")
    assert saved["auto"] is True and saved["version"] == 1
    assert "unsafe" not in saved
    assert journal.pending() == []
    assert deliver_notices(journal, lambda lvl, txt: None) == 1
    # the saved file loads as a normal profile
    spec = load_subagents([proj / ".luna" / "subagents.toml"])["migration-writer"]
    assert spec.auto and spec.max_steps == 20


async def test_two_workdirs_go_to_user_file(auto, journal, tmp_path, isolated_config_home):
    ids = add_runs(journal, 3, workdir=tmp_path / "a") + add_runs(journal, 3,
                                                                    workdir=tmp_path / "b")
    await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    assert table(isolated_config_home / "luna" / "subagents.toml", "migration-writer")
    assert not (tmp_path / "a" / ".luna" / "subagents.toml").exists()


async def test_below_threshold_saves_nothing(auto, journal, tmp_path):
    ids = add_runs(journal, 4, workdir=tmp_path)
    report = await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    assert report.saved == [] and report.rejected[0][0] == "migration-writer"
    assert len(journal.pending()) == 4


async def test_manual_profile_is_never_touched(auto, journal, tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    f.parent.mkdir()
    original = '# mine\n[subagent.migration-writer]\nprompt = "hand"\n'
    f.write_text(original)
    ids = add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    assert report.saved == [] and "hand-written" in report.rejected[0][1]
    assert f.read_text() == original


async def test_builtin_name_is_protected(auto, journal, tmp_path):
    ids = add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(FakeProvider([reply(ids, name="researcher")]), "fast",
                                allow_write=False)
    assert report.saved == []


async def test_auto_profile_update_bumps_version_keeps_comments(auto, journal, clock,
                                                                tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    f.parent.mkdir()
    f.write_text('# keep me\n[subagent.other]\nprompt = "x"  # and me\n')
    await auto.distill(FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]),
                       "fast", allow_write=False)
    clock.now += HOUR
    await auto.distill(FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]),
                       "fast", allow_write=False)
    text = f.read_text()
    assert "# keep me" in text and "# and me" in text
    assert table(f, "migration-writer")["version"] == 2
    assert [v.action for v in journal.versions("migration-writer")] == ["update", "save"]


async def test_write_ceiling_and_unsafe(auto, journal, tmp_path):
    ids = add_runs(journal, 5, workdir=tmp_path, tools=("read_file", "write_file"))
    denied = await auto.distill(FakeProvider([reply(ids, tools=("write_file",))]), "fast",
                                allow_write=False)
    assert denied.saved == [] and "allow_write" in denied.rejected[0][1]
    auto._clock.now += HOUR
    allowed = await auto.distill(FakeProvider([reply(ids, tools=("write_file",))]), "fast",
                                 allow_write=True)
    assert allowed.saved
    assert table(tmp_path / ".luna" / "subagents.toml", "migration-writer")["unsafe"] is True


async def test_hour_gate_and_single_flight(auto, journal, clock, tmp_path):
    ids = add_runs(journal, 6, workdir=tmp_path)
    provider = FakeProvider([reply(ids), reply(ids)])
    first, second = await asyncio.gather(
        auto.distill(provider, "fast", allow_write=False),
        auto.distill(provider, "fast", allow_write=False),
    )
    assert len(provider.requests) == 1
    assert "recently" in (first.skipped or second.skipped)
    clock.now += HOUR - 60
    assert "recently" in (await auto.distill(provider, "fast", allow_write=False)).skipped
    assert len(provider.requests) == 1


async def test_failed_request_still_counts_for_the_gate(auto, journal, clock, tmp_path):
    from luna_core.errors import ProviderError

    class Broken:
        name = "broken"

        async def stream(self, request):
            raise ProviderError("boom")
            yield  # pragma: no cover

    add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(Broken(), "fast", allow_write=False)
    assert "analysis failed" in report.skipped
    assert "recently" in (await auto.distill(Broken(), "fast", allow_write=False)).skipped


async def test_garbage_reply_saves_nothing(auto, journal, tmp_path):
    add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(FakeProvider([FakeProvider.text("sorry, no JSON")]), "fast",
                                allow_write=False)
    assert report.saved == [] and report.rejected == []


async def test_broken_toml_is_not_overwritten(auto, journal, tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    f.parent.mkdir()
    f.write_text("[subagent.x\n")
    ids = add_runs(journal, 6, workdir=tmp_path)
    report = await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    assert report.saved == [] and "cannot read" in report.rejected[0][1]
    assert f.read_text() == "[subagent.x\n"


def test_due_by_count_and_weekly(auto, journal, clock, tmp_path):
    assert not auto.due_weekly()
    add_runs(journal, EVERY - 1, workdir=tmp_path)
    assert not auto.due_by_count() and auto.due_weekly()
    add_runs(journal, 1, workdir=tmp_path)
    assert auto.due_by_count()


async def test_auto_disable(auto, journal, tmp_path):
    ids = add_runs(journal, 5, workdir=tmp_path)
    await auto.distill(FakeProvider([reply(ids)]), "fast", allow_write=False)
    for i in range(5):
        journal.record(workdir=str(tmp_path), parent_session="p", child_session=f"x{i}",
                       base_profile="migration-writer", dynamic=False, spec={"tools": []},
                       task="t", ok=i == 0, cancelled=False, hit_max_steps=False, steps=1,
                       cost=0.0)
    text = auto.check_disable("migration-writer")
    assert text and "1" in text
    f = tmp_path / ".luna" / "subagents.toml"
    assert table(f, "migration-writer")["disabled"] is True
    assert "migration-writer" not in load_subagents([f])
    assert auto.check_disable("migration-writer") is None  # already off


async def test_undo_restores_previous_version_then_removes(auto, journal, clock, tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    await auto.distill(FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]),
                       "fast", allow_write=False)
    clock.now += HOUR
    await auto.distill(FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]),
                       "fast", allow_write=False)
    assert "version 1" in auto.undo("migration-writer")
    assert table(f, "migration-writer")["version"] == 1
    assert "removed" in auto.undo("migration-writer")
    assert "migration-writer" not in tomllib.loads(f.read_text()).get("subagent", {})
    assert "nothing to undo" in auto.undo("migration-writer")


async def test_undo_refuses_after_hand_edit(auto, journal, tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    await auto.distill(FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]),
                       "fast", allow_write=False)
    f.write_text(f.read_text().replace("You write SQL migrations.", "Edited by me."))
    before = f.read_text()
    assert "by hand" in auto.undo("migration-writer")
    assert f.read_text() == before


def test_disable_enable_manual_and_builtin(auto, tmp_path):
    f = tmp_path / ".luna" / "subagents.toml"
    f.parent.mkdir()
    f.write_text('[subagent.mine]\nprompt = "p"\n')
    auto.set_disabled("mine", True, workdir=str(tmp_path))
    assert table(f, "mine")["disabled"] is True
    auto.set_disabled("mine", False, workdir=str(tmp_path))
    assert "disabled" not in table(f, "mine")
    assert "built-in" in auto.set_disabled("researcher", True, workdir=str(tmp_path))


async def test_history_lines(auto, journal, tmp_path):
    await auto.distill(FakeProvider([reply(add_runs(journal, 5, workdir=tmp_path))]),
                       "fast", allow_write=False)
    (line,) = auto.history()
    assert "save" in line and "migration-writer v1" in line
```

`isolated_config_home` — существующая autouse-фикстура из `tests/conftest.py` (возвращает `<home>/.config`); `config_dir()` даёт `<…>/.config/luna`. Если фактический путь `config_dir` отличается — поправь ожидание в `test_two_workdirs_go_to_user_file` на `config_dir() / "subagents.toml"`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_autoprofiles.py -q -o addopts=""`
Expected: FAIL — `ModuleNotFoundError: No module named 'luna.extensions.autoprofiles'`

- [ ] **Step 3: Implement `autoprofiles.py`**

```python
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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tomlkit
from luna_core.assemblies import AssemblyLog, ProfileVersion
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
    """The process-wide journal in ``sessions.db``."""
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


def _profile_data(verdict: Verdict, version: int) -> dict:
    c = verdict.candidate
    data: dict = {"description": c.description, "prompt": c.prompt, "tools": list(c.tools)}
    if c.skills:
        data["skills"] = list(c.skills)
    if c.model:
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
        return config_dir(self._env) / "subagents.toml"

    @staticmethod
    def project_file(workdir: str) -> Path:
        return Path(workdir) / ".luna" / "subagents.toml"

    # --- when ---------------------------------------------------------------

    def wait_left(self) -> float:
        last = self._journal.get_meta(_LAST_TS)
        if last is None:
            return 0.0
        return max(0.0, float(last) + HOUR - self._clock())

    def due_by_count(self) -> bool:
        last = int(self._journal.get_meta(_LAST_ID) or 0)
        return self._journal.count_after(last) >= EVERY

    def due_weekly(self) -> bool:
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
        for candidate in candidates:
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
            verdict = check_candidate(
                candidate, runs_by_id, taken=taken, allow_write=allow_write
            )
            if not verdict.ok:
                report.rejected.append((candidate.name, verdict.reason))
                continue
            try:
                self._save(verdict, path)
            except (_FileError, OSError) as exc:
                report.rejected.append((candidate.name, str(exc)))
                continue
            text = (
                f"Luna сохранила профиль `{candidate.name}` "
                f"({verdict.successes}/{len(verdict.runs)} успешно)"
            )
            self._journal.push_notice("info", text)
            report.saved.append(text)
        return report

    def _taken(self, path: Path) -> dict[str, bool]:
        taken = {name: False for name in BUILTIN_SUBAGENTS}
        for name, cfg in _profiles(_read_doc(path)).items():
            taken[name] = bool(isinstance(cfg, dict) and cfg.get("auto") is True)
        return taken

    def _save(self, verdict: Verdict, path: Path) -> None:
        name = verdict.candidate.name
        doc = _read_doc(path)
        prev = _plain(_profiles(doc).get(name))
        version = int(prev.get("version", 1)) + 1 if prev else 1
        data = _profile_data(verdict, version)
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
            name=name, path=str(path), action="disable",
            version=int(current.get("version", 1)), spec=data, prev=current,
            note="auto: low success",
        )
        good = sum(r.success for r in window)
        text = (
            f"Luna отключила профиль `{name}`: из последних {len(window)} запусков "
            f"успешны {good}"
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
                name=name, path=str(path), action="disable" if disabled else "enable",
                version=int(current.get("version", 1)), spec=data, prev=current,
            )
            return f"{name}: {'disabled' if disabled else 'enabled'} ({path})"
        return (
            f"profile {name!r} is not defined in a subagents.toml "
            "(built-in profiles cannot be disabled here)"
        )

    def undo(self, name: str) -> str:
        """Roll back the newest change of ``name`` that is not undone yet."""
        target: ProfileVersion | None = next(
            (
                v
                for v in self._journal.versions(name, 200)
                if v.action != "undo" and not v.undone
            ),
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
            name=name, path=str(path), action="undo", version=restored,
            spec=target.prev, prev=target.spec,
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
```

Замечания для исполнителя:
- `tomlkit`-значения сравниваются с `dict` из журнала через `_plain` (`unwrap()`), иначе `undo` всегда видел бы «правку руками». Если `unwrap()` в установленной версии `tomlkit` отсутствует — проверь context7 (`tomlkit`), используй актуальный способ получить чистые Python-значения.
- В `test_hour_gate_and_single_flight` оба вызова идут в одном event loop; `_claim` в `to_thread` + `claim_meta` (транзакция `BEGIN IMMEDIATE`) гарантируют одного победителя.
- `check_disable` пишет в `path` из последней версии профиля — профиль мог быть сохранён как в пользовательский, так и в проектный файл.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_autoprofiles.py -q -o addopts=""`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add luna/extensions/autoprofiles.py tests/test_autoprofiles.py
git commit -m "feat(subagents): auto-profiles — distill, save, versions, undo, auto-disable"
```

---

### Task 5: Подключение и триггеры

**Files:**
- Modify: `luna/core/agent.py` (раннер получает `journal` и `on_recorded`; `roles` вычисляются до раннера)
- Modify: `luna/server/runtime.py` (`SessionRuntime.after_turn` доставляет уведомления)
- Modify: `luna/server/app.py` (`lifespan`: недельный анализ в фоне)
- Modify: `luna/core/session.py` (`run_repl`: печать уведомлений при старте и после каждого хода)
- Modify: `tests/conftest.py` (`isolated_config_home` вызывает `close_journals()`)
- Test: `tests/test_autoprofiles_wiring.py`

**Interfaces:**
- Consumes: `open_journal`, `AutoProfiles`, `deliver_notices`, `close_journals` (Task 4); `SubagentRunner.journal/on_recorded` (Task 2).
- Produces:
  - `luna.extensions.autoprofiles.make_on_recorded(auto: AutoProfiles, *, fast: tuple[object, str], allow_write: bool) -> Callable[[RunRecord, Session], None]` — синхронный колбэк:
    1. если `record.base_profile` есть в журнале версий → `auto.check_disable(record.base_profile)` (результат уже в `luna_notices`);
    2. если `auto.due_by_count()` и `auto.wait_left() == 0` и нет живой фоновой задачи → `asyncio.get_running_loop().create_task(auto.distill(*fast, allow_write=allow_write))`, ссылка держится в модульном `set` до завершения; исключение задачи логируется (`_log.exception`) и не всплывает.
  - `luna.extensions.autoprofiles.weekly_distill(config) -> None` (async) — для сервера: `auto = AutoProfiles(open_journal())`; если не `due_weekly()` → выход; иначе собрать провайдера роли `fast` из `config` (`build_provider` + `_roles` из `luna.core.agent`; `roles.get("fast", provider, model)`), вызвать `distill`; `LunaConfigError` и любые ошибки — `_log.warning`, без падения.

- [ ] **Step 1: Write the failing tests**

```python
"""Wiring: runs reach the journal, triggers fire, notices are delivered."""

import asyncio

from luna_core.assemblies import AssemblyLog
from luna_core.providers.fake import FakeProvider

from luna.extensions import autoprofiles
from luna.extensions.autoprofiles import AutoProfiles, make_on_recorded


async def test_build_agent_journals_subagent_runs(tmp_path, monkeypatch):
    from luna.config.config import LunaConfig
    from luna.core.agent import build_agent

    provider = FakeProvider(
        [
            FakeProvider.call("task", {"subagent_type": "researcher", "description": "look"}),
            FakeProvider.text("child done"),
            FakeProvider.text("parent done"),
        ]
    )
    bundle = await build_agent(LunaConfig(workdir=str(tmp_path)), provider=provider)
    runner = bundle.agent.tools["task"].runner
    assert runner.journal is autoprofiles.open_journal()
    assert runner.on_recorded is not None


async def test_on_recorded_starts_one_background_distill(tmp_path, monkeypatch):
    journal = AssemblyLog(tmp_path / "s.db")
    auto = AutoProfiles(journal)
    calls = []

    async def fake_distill(provider, model, *, allow_write):
        calls.append((model, allow_write))
        await asyncio.sleep(0)

    monkeypatch.setattr(auto, "distill", fake_distill)
    monkeypatch.setattr(autoprofiles, "EVERY", 2)
    hook = make_on_recorded(auto, fast=(object(), "fast-model"), allow_write=False)
    for i in range(3):
        rec = journal.record(workdir="/w", parent_session="p", child_session=f"c{i}",
                             base_profile="gp", dynamic=True, spec={}, task="t", ok=True,
                             cancelled=False, hit_max_steps=False, steps=1, cost=0.0)
        hook(rec, None)
    await asyncio.sleep(0.01)
    assert calls == [("fast-model", False)]  # one live task at a time
    journal.close()


async def test_on_recorded_checks_auto_disable(tmp_path, monkeypatch):
    journal = AssemblyLog(tmp_path / "s.db")
    auto = AutoProfiles(journal)
    seen = []
    monkeypatch.setattr(auto, "check_disable", lambda name: seen.append(name))
    journal.add_version(name="mw", path=str(tmp_path / "x.toml"), action="save", version=1,
                        spec={}, prev=None)
    hook = make_on_recorded(auto, fast=(object(), "m"), allow_write=False)
    rec = journal.record(workdir="/w", parent_session="p", child_session="c",
                         base_profile="mw", dynamic=False, spec={}, task="t", ok=False,
                         cancelled=False, hit_max_steps=False, steps=1, cost=0.0)
    hook(rec, None)
    other = journal.record(workdir="/w", parent_session="p", child_session="d",
                           base_profile="researcher", dynamic=False, spec={}, task="t",
                           ok=False, cancelled=False, hit_max_steps=False, steps=1, cost=0.0)
    hook(other, None)
    assert seen == ["mw"]
    journal.close()


async def test_runtime_after_turn_delivers_notices(monkeypatch):
    from luna_core.events import Notice

    from luna.server import runtime as runtime_mod

    journal = autoprofiles.open_journal()
    journal.push_notice("info", "Luna сохранила профиль `x` (5/5 успешно)")
    notified = []

    class FakeSession:
        def notify(self, event):
            notified.append(event)

    rt = runtime_mod.SessionRuntime.__new__(runtime_mod.SessionRuntime)
    rt.session = FakeSession()
    rt.reload_requested = rt.reload_after_turn = False
    monkeypatch.setattr(rt, "save", lambda: None, raising=False)
    await rt.after_turn()
    assert [e.text for e in notified if isinstance(e, Notice)] == [
        "Luna сохранила профиль `x` (5/5 успешно)"
    ]


async def test_weekly_distill_skips_without_records(monkeypatch):
    from luna.config.config import LunaConfig

    called = []
    monkeypatch.setattr(AutoProfiles, "distill",
                        lambda *a, **k: called.append(1))
    await autoprofiles.weekly_distill(LunaConfig())
    assert called == []
```

Тест `test_build_agent_journals_subagent_runs` проверяет только подключение; если `LunaConfig(...)` в тестах создаётся иначе (фабрика/фикстура в `tests/test_agent.py`) — используй тот же способ. Тест `test_runtime_after_turn_delivers_notices` строит рантайм в обход конструктора; если `after_turn` трогает другие атрибуты — задай их так же через `monkeypatch`/присваивание, не меняя проверяемого поведения. `Notice` в рантайме — тот, что импортирован в `luna/server/runtime.py`; сверяй класс по нему.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_autoprofiles_wiring.py -q -o addopts=""`
Expected: FAIL — `ImportError: cannot import name 'make_on_recorded'`

- [ ] **Step 3: Implement the hook and weekly run in `autoprofiles.py`**

```python
import logging

_log = logging.getLogger(__name__)
_TASKS: set[asyncio.Task] = set()


def make_on_recorded(auto: AutoProfiles, *, fast: tuple[object, str], allow_write: bool):
    """The runner's ``on_recorded``: auto-disable check, then the every-``EVERY`` trigger."""

    def on_recorded(record, session) -> None:
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
        _log.warning("background distill failed: %s", task.exception())


async def weekly_distill(config) -> None:
    """Server start: run the analysis when a week has passed and there is something new."""
    from luna.config.providers import LunaConfigError, build_provider, merge_providers
    from luna.core.agent import _roles

    auto = AutoProfiles(open_journal())
    if not auto.due_weekly():
        return
    try:
        registry = merge_providers(config.custom_providers)
        provider, model = build_provider(config.provider, config.model, registry=registry)
        fast = _roles(config, registry).get("fast", provider, model)
        await auto.distill(*fast, allow_write=config.subagents_allow_write)
    except LunaConfigError as exc:
        _log.warning("weekly distill skipped: %s", exc)
    except Exception:  # noqa: BLE001 - never take the server down
        _log.exception("weekly distill failed")
```

(Импорты `logging` — в шапку модуля; `make_on_recorded`, `weekly_distill` — в `__all__`. Тест на один фоновый запуск: `_TASKS` держит живую задачу; `monkeypatch` `EVERY` работает, потому что `due_by_count` читает модульную `EVERY` при вызове — убедись, что это так, а не значение по умолчанию аргумента.)

- [ ] **Step 4: Wire `build_agent`**

В `luna/core/agent.py` перенеси `roles = _roles(config, registry)` **перед** созданием `SubagentRunner` и передай:

```python
        from luna.extensions.autoprofiles import AutoProfiles, make_on_recorded, open_journal

        journal = open_journal()
        runner = SubagentRunner(
            load_subagents(str(workdir), on_warn=on_warn),
            max_parallel=config.max_parallel,
            dynamic=DynamicSettings(...),  # как сейчас
            skill_loader=...,  # как сейчас
            skill_names=...,  # как сейчас
            journal=journal,
            on_recorded=make_on_recorded(
                AutoProfiles(journal),
                fast=roles.get("fast", provider, model),
                allow_write=config.subagents_allow_write,
            ),
        )
```

(Ленивый импорт внутри функции — по соглашению проекта для тяжёлых модулей; `autoprofiles` тянет `tomlkit`.)

- [ ] **Step 5: Deliver notices in the server and the REPL; weekly run at server start**

`luna/server/runtime.py`, в начале `after_turn` после `self.save()`:

```python
        from luna.extensions.autoprofiles import deliver_notices, open_journal

        try:
            deliver_notices(open_journal(), lambda level, text: self.session.notify(Notice(level, text)))
        except Exception:  # noqa: BLE001 - notices must never break a finished turn
            _log.exception("delivering notices failed")
```

(Если в модуле нет `_log` — добавь `_log = logging.getLogger(__name__)`.)

`luna/server/app.py`, `lifespan`:

```python
    @contextlib.asynccontextmanager
    async def lifespan(app):
        from luna.config.config import load_config
        from luna.extensions.autoprofiles import weekly_distill

        weekly = None
        with contextlib.suppress(Exception):
            weekly = asyncio.get_running_loop().create_task(weekly_distill(load_config()))
        yield
        if weekly is not None and not weekly.done():
            weekly.cancel()
        await shutdown(app)
```

Чтобы тесты сервера не делали этого, добавь в `create_app` параметр `startup_distill: bool = True` и вызывай `weekly_distill` только при `True`; в `run_serve` — по умолчанию; в тестах сервера, которые создают `create_app`, ничего менять не нужно, если `weekly_distill` без записей в журнале сразу выходит (а он выходит: `due_weekly()` = False при пустом журнале). Выбери один из двух путей и опиши в отчёте.

`luna/core/session.py`, `run_repl`: напечатай накопленные уведомления при старте REPL и после каждого завершённого хода (там, где REPL печатает итог хода) через существующую консоль:

```python
def _print_notices(console) -> None:
    from luna.extensions.autoprofiles import deliver_notices, open_journal

    with contextlib.suppress(Exception):
        deliver_notices(open_journal(), lambda level, text: console.print(f"[{level}] {text}", markup=False))
```

(Форму вывода подгони под то, как REPL уже печатает `Notice` — переиспользуй существующую функцию рендера уведомлений, если она есть.)

`tests/conftest.py`, в `isolated_config_home` рядом с закрытием `store._LOGS`:

```python
    from luna.extensions.autoprofiles import close_journals

    close_journals()
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_autoprofiles_wiring.py tests/test_agent.py tests -q -o addopts="" -x`
Expected: PASS (весь набор приложения: REPL/сервер не сломаны)

- [ ] **Step 7: Commit**

```bash
git add luna/extensions/autoprofiles.py luna/core/agent.py luna/server/runtime.py luna/server/app.py luna/core/session.py tests/conftest.py tests/test_autoprofiles_wiring.py
git commit -m "feat(subagents): journal every run, distill every 20 runs and weekly, deliver notices"
```

---

### Task 6: Команды `luna subagents …` и `/subagents …`, документация

**Files:**
- Modify: `luna/cli.py` (`_SUBCOMMANDS`, `handlers`, новая `_run_subagents`)
- Modify: `luna/commands/builtin.py` (новая команда `/subagents`)
- Modify: `README.md` (раздел про `[subagents]` — подраздел «Автопрофили»), `AGENTS.md` (строки про `assemblies.py`, `distill.py`, `autoprofiles.py`)
- Test: `tests/test_cli_subagents.py`, `tests/test_commands.py` (или файл, где тестируются builtin-команды — найди по `"/agents"`)

**Interfaces:**
- Consumes: `AutoProfiles`, `open_journal`, `deliver_notices`, `DistillReport.lines()` (Task 4); `_roles` из `luna.core.agent`; `build_provider`, `merge_providers` из `luna.config.providers`; `load_config` из `luna.config.config`.
- Produces:
  - CLI: `luna subagents distill | history [name] | undo <name> | disable <name> | enable <name>`; без аргумента — `history`. Код возврата 0; `undo`/`disable`/`enable` с ошибочным именем — печать причины, код 1.
  - Slash: `/subagents` (kind `mutate`), подкоманды те же; без аргумента — `history`. `distill` берёт `env.agent.roles.get("fast", env.agent.provider, env.agent.model)` и `env.config.subagents_allow_write`; `disable/enable` — `workdir=env.workdir`.
  - Хелпер `luna.extensions.autoprofiles.fast_target(config) -> tuple[object, str]` (вынести из `weekly_distill`, переиспользовать там).

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_subagents.py`:

```python
"""luna subagents: history / undo / disable / enable / distill."""

from luna.cli import main
from luna.extensions import autoprofiles
from luna.extensions.autoprofiles import open_journal


def test_history_empty(capsys):
    assert main(["subagents"]) == 0
    assert "no profile changes yet" in capsys.readouterr().out


def test_disable_enable_and_unknown(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / ".luna" / "subagents.toml"
    f.parent.mkdir()
    f.write_text('[subagent.mine]\nprompt = "p"\n')
    assert main(["subagents", "disable", "mine"]) == 0
    assert "disabled = true" in f.read_text()
    assert main(["subagents", "enable", "mine"]) == 0
    assert main(["subagents", "disable", "researcher"]) == 1
    assert "built-in" in capsys.readouterr().out


def test_undo_nothing(capsys):
    assert main(["subagents", "undo", "ghost"]) == 1
    assert "nothing to undo" in capsys.readouterr().out


def test_distill_uses_fast_role_and_prints_report(monkeypatch, capsys):
    from luna_core.providers.fake import FakeProvider

    provider = FakeProvider([FakeProvider.text('{"profiles": []}')])
    monkeypatch.setattr(autoprofiles, "fast_target", lambda config: (provider, "fast-m"))
    open_journal().record(workdir="/w", parent_session="p", child_session="c",
                          base_profile="gp", dynamic=True, spec={}, task="t", ok=True,
                          cancelled=False, hit_max_steps=False, steps=1, cost=0.0)
    assert main(["subagents", "distill"]) == 0
    assert "no repeating assemblies found" in capsys.readouterr().out
    assert provider.requests[0].model == "fast-m"
    assert main(["subagents", "distill"]) == 0  # within the hour
    assert "recently" in capsys.readouterr().out
    assert len(provider.requests) == 1
```

В файле тестов builtin-команд — тест в стиле соседних тестов `/agents` (найди, как там строится `env`):

```python
async def test_subagents_command_history_and_distill(...):
    # env с FakeProvider-агентом, как в соседних тестах /agents
    result = await run_command(env, "/subagents")
    assert "no profile changes yet" in result.text
    result = await run_command(env, "/subagents distill")
    assert "nothing new in the journal" in result.text  # пустой журнал: запроса нет
    assert env.agent.provider.requests == []
```

(Подставь фактические имена хелперов этого файла: как он вызывает команду и собирает `env`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_subagents.py -q -o addopts=""`
Expected: FAIL — `luna subagents` не распознаётся (argparse ошибка / код 2)

- [ ] **Step 3: Implement**

`luna/extensions/autoprofiles.py`:

```python
def fast_target(config) -> tuple[object, str]:
    """Provider and model of the ``fast`` role for ``config`` (raises LunaConfigError)."""
    from luna.config.providers import build_provider, merge_providers
    from luna.core.agent import _roles

    registry = merge_providers(config.custom_providers)
    provider, model = build_provider(config.provider, config.model, registry=registry)
    return _roles(config, registry).get("fast", provider, model)
```

и в `weekly_distill` заменить сборку провайдера на `fast = fast_target(config)`.

`luna/cli.py`: добавить `"subagents"` в `_SUBCOMMANDS` и `"subagents": _run_subagents` в `handlers`.

```python
def _run_subagents(argv: list[str]) -> int:
    import asyncio

    from luna.config.config import load_config
    from luna.extensions import autoprofiles

    parser = argparse.ArgumentParser(prog="luna subagents")
    parser.add_argument(
        "cmd", nargs="?", default="history",
        choices=["distill", "history", "undo", "disable", "enable"],
    )
    parser.add_argument("name", nargs="?")
    args = parser.parse_args(argv)
    journal = autoprofiles.open_journal()
    auto = autoprofiles.AutoProfiles(journal)
    if args.cmd == "history":
        lines = auto.history(args.name) or ["no profile changes yet"]
        print("\n".join(lines))
        return 0
    if args.cmd == "distill":
        config = load_config()
        report = asyncio.run(
            auto.distill(
                *autoprofiles.fast_target(config),
                allow_write=config.subagents_allow_write,
            )
        )
        journal.drain_notices()  # printed here; do not repeat them after the next turn
        print("\n".join(report.lines()))
        return 0
    if not args.name:
        parser.error(f"{args.cmd} needs a profile name")
    if args.cmd == "undo":
        text = auto.undo(args.name)
        ok = not text.startswith(("nothing to undo", "cannot read")) and "by hand" not in text
    else:
        text = auto.set_disabled(args.name, args.cmd == "disable", workdir=os.getcwd())
        ok = text.startswith(f"{args.name}: ")
    print(text)
    return 0 if ok else 1
```

`luna/commands/builtin.py`:

```python
@register("/subagents", "auto-profiles: distill | history | undo|disable|enable <name>", "mutate", agent=True)
async def _subagents(env, arg):
    from luna.extensions.autoprofiles import AutoProfiles, open_journal

    journal = open_journal()
    auto = AutoProfiles(journal)
    cmd, _, name = (arg or "").strip().partition(" ")
    cmd, name = cmd or "history", name.strip()
    if cmd == "history":
        return CommandResult(text="\n".join(auto.history(name or None)) or "no profile changes yet")
    if cmd == "distill":
        agent = env.agent
        provider, model = agent.roles.get("fast", agent.provider, agent.model)
        report = await auto.distill(provider, model, allow_write=env.config.subagents_allow_write)
        journal.drain_notices()
        return CommandResult(text="\n".join(report.lines()))
    if cmd in ("undo", "disable", "enable") and name:
        text = (
            auto.undo(name)
            if cmd == "undo"
            else auto.set_disabled(name, cmd == "disable", workdir=env.workdir)
        )
        return CommandResult(text=text)
    return CommandResult(notices=[Notice("error", "usage: /subagents [distill|history [name]|undo|disable|enable <name>]")])
```

(Проверь, поддерживает ли `register` async-обработчики — в проекте команды async; если нужна иная сигнатура или `env.config` называется иначе, подстрой под соседние команды. `/subagents` должен попасть в автодополнение TUI — это делается через общий `HELP`; убедись, что тест списка команд, если он сверяет полный набор, обновлён.)

- [ ] **Step 4: Docs**

`README.md`, в раздел `[subagents]` добавь подраздел:

```markdown
#### Автопрофили

Каждый запуск субагента пишется в журнал (`sessions.db`): сборка, которую дал
главный агент, и первые 200 символов задачи — без вывода инструментов и
ответов. Раз в 20 запусков, раз в неделю при старте сервера или по команде
`luna subagents distill` Luna одним запросом к модели роли `fast` ищет
повторяющиеся сборки. Группа из ≥ 5 запусков с ≥ 70 % успешных сохраняется как
профиль с `auto = true`: в `~/.config/luna/subagents.toml`, если встречалась в
двух и более проектах, иначе в `.luna/subagents.toml` проекта. Анализ — не чаще
раза в час. Ручные профили он не меняет; пишущие права не выше `allow_write`.

Если auto-профиль в последних запусках чаще проваливается (меньше 50 % из
последних 10), Luna его отключает и сообщает об этом.

    luna subagents                 # история изменений профилей
    luna subagents distill         # анализ сейчас
    luna subagents undo <name>     # откатить последнее изменение
    luna subagents disable <name>  # выключить / enable — включить

То же в TUI и REPL: `/subagents …`.
```

`AGENTS.md`, в «Структура»: строки для `packages/luna-core/src/luna_core/assemblies.py` (журнал сборок), `distill.py` (анализ журнала, проверка кандидатов), `luna/extensions/autoprofiles.py` (автопрофили: сохранение, версии, откат, автоотключение, часовой лимит).

- [ ] **Step 5: Full verification**

Run:
```bash
uv run pytest -q -o addopts=""
uv run ruff check .
uv run ruff format --check .
```
Expected: всё зелёное, ruff чистый.

- [ ] **Step 6: Commit**

```bash
git add luna/cli.py luna/commands/builtin.py luna/extensions/autoprofiles.py README.md AGENTS.md tests/test_cli_subagents.py tests/test_commands.py
git commit -m "feat(subagents): luna subagents and /subagents commands; docs"
```

---

## Self-Review (выполнено при написании)

- **Покрытие спеки 1b:** таблица журнала — Task 1 (+ колонка `mutating_json`, решение выше); `task_head` 200, без вывода — Tasks 1–2; успех — Task 1; триггеры: 20 записей — Task 5, неделя при старте сервера — Task 5, вручную — Task 6, после команды — план 2 (публичный `distill`); последние 200 без `profile_name` — Task 4; один запрос к `fast` с потолком (`max_tokens=4000`, вход ≤ 60 000 символов) — Task 3; проверки 5 / 70 %, инструменты, имя, `allow_write` — Task 3; куда сохранять, `auto`, `version`, пометка записей — Task 4; уведомление — Task 4 (текст), Task 5 (доставка); ручные не трогаются, автоотключение, история, `undo`, `disable/enable`, `/subagents` — Tasks 4–6; не чаще раза в час — Task 4.
- **Тесты из спеки:** журнал без вывода — Task 2; пороги — Task 3; ручной профиль — Task 4; автоотключение — Tasks 3–4; `undo` — Task 4; мусорный JSON — Tasks 3–4.
- **Согласованность имён:** `AssemblyLog`, `RunRecord.success`, `pending`, `mark`, `recent_for`, `claim_meta`, `drain_notices`; `check_candidate(..., taken=, allow_write=)`; `AutoProfiles.distill(provider, model, *, allow_write)`; `make_on_recorded`, `weekly_distill`, `fast_target`, `deliver_notices`, `open_journal`, `close_journals` — одинаковы во всех задачах.
