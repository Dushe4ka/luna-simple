# Динамические субагенты и `resume` — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** главный агент через инструмент `task` сам собирает субагента (промпт,
подмножество своих инструментов, скилы, модель, лимит шагов, контекст) и может
продолжить прошлый запуск субагента по `resume`.

**Architecture:** вся логика — в `luna-core`.

- `subagents.py`:
  - `SubagentSpec` получает новые поля и (де)сериализацию;
  - `SubagentRunner.run` принимает готовую сборку и `resume`;
  - системный промпт ребёнка собирается из промпта, контекста и скилов;
  - результат несёт метрики (шаги, стоимость, исход).
- Новый модуль `dynamic.py`: `resolve_spec` проверяет поля вызова против
  инструментов родителя и настроек `DynamicSettings`.
- `tools/tasks.py`: расширенная схема `task`.
- Приложение (`luna/config`, `luna/core/agent.py`): раздел `[subagents]` конфига
  и передача загрузчика скилов.

**Tech Stack:** Python 3.11+, asyncio, sqlite (`EventLog`), pytest
(`asyncio_mode=auto`), ruff.

**Spec:** `docs/superpowers/specs/2026-11-16-luna-dynamic-subagents-teams-design.md`
(Часть 1, разделы 1.1–1.4). Части 1b и 2 — следующие планы.

## Global Constraints

- Python 3.11+, PEP 8 / PEP 257, `uv run ruff check .` и `uv run ruff format --check .`
  чистые.
- Тесты не ходят в сеть: только `FakeProvider` (`luna_core.providers.fake`).
- ID моделей — только в конфиге / `luna/config/providers.py`, никогда в логике агента.
- Старые вызовы `task` (только `description`, `subagent_type`, `background`,
  `isolation`) работают как раньше; все существующие тесты остаются зелёными.
- Ребёнку **никогда** не выдаются: `task`, `task_status`, `task_cancel`,
  `manage_mcp`, `manage_skills`, `save_skill` (глубина 1).
- `tools` динамической сборки — только подмножество инструментов родителя.
- Пишущие инструменты (`write_file`, `edit_file`, `delete`, `execute` и любой
  `Tool.mutating`) в динамической сборке — только при `[subagents].allow_write = true`.
- Политика, подтверждения, `/undo`, worktree-изоляция и `permissions.toml` работают
  как сейчас; сборка не может их ослабить.
- Значения по умолчанию `[subagents]`: `dynamic = true`, `allow_write = false`,
  `models = []`, `max_steps = 30`.
- В сообщениях об ошибках для модели — английский (как во всём `luna-core`); в
  README — русский.
- Команды: тесты `uv run pytest`, ядро отдельно
  `uv run pytest packages/luna-core/tests`.

## Review Focus

1. Проектный `.luna.toml` в склонированном репозитории ставит
   `[subagents] allow_write = true` или свои `models`. Ожидание: проектный слой
   может только **сузить** права (`allow_write = false`, `dynamic = false`).
   Расширение игнорируется с предупреждением в `config.warnings`. Тест — Task 6.
2. Модель передаёт `tools: []` или `prompt: ""`. Ожидание: понятная ошибка, а не
   субагент без инструментов или с пустым промптом. Тест — Task 2.
3. `resume` чужой сессии (другого родителя, обычной, форка) или субагента, который
   ещё работает в фоне. Ожидание: ошибка, чужой разговор не продолжается. Тест —
   Task 4.
4. Динамическая сборка поверх `general-purpose` (у него пишущие инструменты) при
   `allow_write = false` и без явного `tools`. Ожидание: пишущие инструменты базы
   отбрасываются, субагент только читает, а не падает. Тест — Task 2.
5. Профиль из `subagents.toml` перечисляет `task` или инструмент, которого у хоста
   нет. Ожидание: `task` молча не выдаётся. Отсутствующий инструмент — notice
   `warn`, а не падение. Тест — Task 3.

---

## Карта файлов

| Файл | Что меняется |
|---|---|
| `packages/luna-core/src/luna_core/subagents.py` | `SubagentSpec` (+поля, `to_dict`/`from_dict`), `CHILD_FORBIDDEN`, `DynamicSettings`, загрузчик TOML, `SubagentResult` (+метрики, подсказка resume), `SubagentRunner` (сборка, промпт, модель по роли, resume), `create_worktree(branch=, base=)` |
| `packages/luna-core/src/luna_core/dynamic.py` | **новый**: `SubagentRequestError`, `DYNAMIC_FIELDS`, `resolve_spec` |
| `packages/luna-core/src/luna_core/tools/tasks.py` | схема и логика `task` |
| `packages/luna-core/src/luna_core/__init__.py` | экспорт `DynamicSettings`, `resolve_spec` |
| `packages/luna-core/tests/test_subagents.py` | тесты загрузчика, раннера, resume |
| `packages/luna-core/tests/test_dynamic.py` | **новый**: тесты `resolve_spec` и инструмента `task` |
| `packages/luna-core/tests/test_public_api.py` | ожидаемый набор экспорта |
| `luna/config/config.py` | раздел `[subagents]` → поля `LunaConfig` |
| `luna/core/agent.py` | `SubagentRunner(dynamic=…, skill_loader=…, skill_names=…)` |
| `luna/commands/builtin.py` | `/agents` показывает режим динамических субагентов |
| `tests/test_config.py`, `tests/test_agent.py` | тесты конфига и сборки агента |
| `README.md` | раздел про `[subagents]` и поля `task` |

---

### Task 1: `SubagentSpec` — новые поля, сериализация, загрузчик

**Files:**
- Modify: `packages/luna-core/src/luna_core/subagents.py` (константы вверху,
  `SubagentSpec` ~стр. 69–86, `_check_types` ~114–139, `load_subagents` ~142–196)
- Test: `packages/luna-core/tests/test_subagents.py`

**Interfaces:**
- Produces:
  - `SubagentSpec(..., skills: tuple[str, ...] = (), context: str = "", auto: bool = False, disabled: bool = False, version: int = 1)`.
    Новые поля идут **после** `max_cost`, чтобы позиционные вызовы не сломались.
  - `SubagentSpec.to_dict() -> dict`, `SubagentSpec.from_dict(data: Mapping) -> SubagentSpec`.
  - `CHILD_FORBIDDEN: frozenset[str]`.
  - `DynamicSettings(enabled=True, allow_write=False, models=(), max_steps=30)`
    (frozen dataclass).
  - `load_subagents` больше не отвергает незнакомые имена инструментов (MCP/LSP
    проверяются при запуске). Отвергает только `CHILD_FORBIDDEN`. Читает `skills`,
    `context`, `auto`, `disabled`, `version`. Профили с `disabled = true` не
    попадают в результат.

- [ ] **Step 1: Write the failing tests** (добавить в конец `test_subagents.py`)

```python
from luna_core.subagents import CHILD_FORBIDDEN, DynamicSettings


def test_spec_round_trips_through_a_dict():
    spec = SubagentSpec(
        "mig", "migrations", "p", ("read_file", "mcp__db__query"), "fast",
        skills=("sql",), context="db is postgres", auto=True, version=3,
    )
    data = spec.to_dict()
    assert data["tools"] == ["read_file", "mcp__db__query"] and data["skills"] == ["sql"]
    assert SubagentSpec.from_dict(data) == spec
    # unknown keys (from a newer Luna) are ignored, missing ones take defaults
    assert SubagentSpec.from_dict({"name": "x", "description": "d", "prompt": "p", "new": 1}) == (
        SubagentSpec("x", "d", "p")
    )


def test_loader_accepts_mcp_tools_skills_and_context(tmp_path):
    f = tmp_path / "s.toml"
    f.write_text(
        '[subagent.db]\ntools = ["read_file", "mcp__db__query"]\n'
        'skills = ["sql"]\ncontext = "postgres 16"\nauto = true\nversion = 2\n'
    )
    spec = load_subagents([f])["db"]
    assert spec.tools == ("read_file", "mcp__db__query")
    assert (spec.skills, spec.context, spec.auto, spec.version) == (("sql",), "postgres 16", True, 2)


def test_loader_rejects_delegation_tools_and_skips_disabled(tmp_path):
    f = tmp_path / "s.toml"
    f.write_text('[subagent.x]\ntools = ["read_file", "task"]\n')
    with pytest.raises(SubagentConfigError, match="cannot use"):
        load_subagents([f])
    f.write_text('[subagent.off]\nprompt = "p"\ndisabled = true\n')
    assert "off" not in load_subagents([f])


def test_loader_type_checks_the_new_keys(tmp_path):
    f = tmp_path / "s.toml"
    f.write_text('[subagent.x]\nskills = "sql"\n')
    with pytest.raises(SubagentConfigError, match="skills must be a list of strings"):
        load_subagents([f])
    f.write_text('[subagent.x]\nauto = "yes"\n')
    with pytest.raises(SubagentConfigError, match="auto must be a boolean"):
        load_subagents([f])


def test_dynamic_settings_defaults_and_forbidden_set():
    assert DynamicSettings() == DynamicSettings(True, False, (), 30)
    assert CHILD_FORBIDDEN == {
        "task", "task_status", "task_cancel", "manage_mcp", "manage_skills", "save_skill",
    }
```

Также поменять одну проверку в существующем `test_load_subagents_validation`.
Незнакомый инструмент теперь загружается:

```python
    bad.write_text('[subagent.x]\ntools = ["teleport"]\n')
    assert load_subagents([bad])["x"].tools == ("teleport",)
```

(заменяет блок `with pytest.raises(SubagentConfigError, match="unknown tools")`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_subagents.py -q -k "round_trips or mcp_tools or delegation_tools or new_keys or forbidden_set or validation"`
Expected: FAIL (`ImportError: cannot import name 'CHILD_FORBIDDEN'`).

- [ ] **Step 3: Implement**

Вверху `subagents.py` (рядом с `READ_ONLY`):

```python
from collections.abc import Callable, Collection, Mapping

#: Never given to a subagent: no delegation, no extension changes (depth 1).
CHILD_FORBIDDEN = frozenset(
    {"task", "task_status", "task_cancel", "manage_mcp", "manage_skills", "save_skill"}
)
```

`VALID_TOOLS` оставить: он больше не используется загрузчиком, но остаётся
частью модуля для совместимости.

`SubagentSpec`:

```python
@dataclass(frozen=True)
class SubagentSpec:
    """A subagent definition."""

    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] = READ_ONLY
    model: str | None = None
    unsafe: bool = False
    isolation: str = "auto"
    max_steps: int = 30
    max_cost: float | None = None
    skills: tuple[str, ...] = ()
    context: str = ""
    auto: bool = False
    disabled: bool = False
    version: int = 1

    @property
    def writes(self) -> bool:
        """True when it may change files or run commands."""
        return bool(set(self.tools) & set(WRITE_TOOLS)) or self.unsafe

    def to_dict(self) -> dict:
        """JSON-friendly form (session metadata, the assembly log)."""
        data = dataclasses.asdict(self)
        data["tools"] = list(self.tools)
        data["skills"] = list(self.skills)
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> SubagentSpec:
        """Inverse of :meth:`to_dict`; unknown keys are ignored."""
        known = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        for key in ("tools", "skills"):
            if key in values:
                values[key] = tuple(values[key])
        return cls(**values)


@dataclass(frozen=True)
class DynamicSettings:
    """``[subagents]``: may the parent assemble subagents, and within which limits."""

    enabled: bool = True
    allow_write: bool = False
    models: tuple[str, ...] = ()
    max_steps: int = 30
```

`_check_types`: в словарь `checks` добавить

```python
        "context": (str, "a string"),
        "auto": (bool, "a boolean (auto = true or auto = false)"),
        "disabled": (bool, "a boolean (disabled = true or disabled = false)"),
        "version": (int, "an integer"),
```

и после проверки `tools` добавить такую же для `skills`:

```python
    skills = cfg.get("skills", [])
    if not isinstance(skills, (list, tuple)) or not all(isinstance(s, str) for s in skills):
        raise SubagentConfigError(
            f"{path}: [subagent.{name}] skills must be a list of strings, got {skills!r}"
        )
```

В `load_subagents` заменить блок `unknown = set(tools) - VALID_TOOLS ...` на:

```python
            forbidden = set(tools) & CHILD_FORBIDDEN
            if forbidden:
                raise SubagentConfigError(
                    f"subagent {name!r} cannot use {sorted(forbidden)}: "
                    "subagents do not delegate or change extensions"
                )
```

Создание спеки (`specs[name] = SubagentSpec(...)`) переписать с ключевыми
аргументами и пропуском отключённых:

```python
            if cfg.get("disabled") is True:
                specs.pop(name, None)
                continue
            specs[name] = SubagentSpec(
                name,
                str(cfg.get("description", name)),
                str(cfg.get("prompt", f"You are the {name} subagent.")),
                tools,
                cfg.get("model"),
                bool(cfg.get("unsafe", False)),
                isolation,
                int(cfg.get("max_steps", 30)),
                float(cfg["max_cost"]) if "max_cost" in cfg else None,
                skills=tuple(cfg.get("skills", ())),
                context=str(cfg.get("context", "")),
                auto=bool(cfg.get("auto", False)),
                version=int(cfg.get("version", 1)),
            )
```

(`disabled = true` для встроенного имени, например `researcher`, убирает и его. Так
пользователь может выключить встроенный профиль.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/luna-core/tests/test_subagents.py -q`
Expected: PASS (все, включая старые).

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/subagents.py packages/luna-core/tests/test_subagents.py
git commit -m "feat(subagents): spec gets skills, context, auto/disabled/version; loader accepts any tool"
```

---

### Task 2: `resolve_spec` — сборка из полей вызова

**Files:**
- Create: `packages/luna-core/src/luna_core/dynamic.py`
- Modify: `packages/luna-core/src/luna_core/__init__.py`, `packages/luna-core/tests/test_public_api.py`
- Test: `packages/luna-core/tests/test_dynamic.py`

**Interfaces:**
- Consumes: `SubagentSpec`, `DynamicSettings`, `CHILD_FORBIDDEN`, `WRITE_TOOLS`,
  `READ_ONLY` (Task 1); `luna_core.roles.ROLES`.
- Produces:
  - `class SubagentRequestError(LunaCoreError)`.
  - `DYNAMIC_FIELDS = ("prompt", "tools", "skills", "model", "context")`.
  - `resolve_spec(base: SubagentSpec, args: Mapping[str, object], *, parent_tools: Mapping[str, object], skills: Collection[str], settings: DynamicSettings) -> tuple[SubagentSpec, bool]`.
    Второй элемент — `True` для динамической сборки.
  - `is_writing(name: str, parent_tools: Mapping[str, object]) -> bool`.
  - Экспорт из `luna_core`: `DynamicSettings`, `resolve_spec`.

Правила (из спецификации 1.1):

- Сборка **динамическая**, если в `args` есть непустое поле из `DYNAMIC_FIELDS`.
- Только `max_steps` без этих полей — не динамическая сборка: база с другим
  лимитом шагов.
- Динамическая при `settings.enabled = False` → ошибка.
- `tools` не задан → берутся инструменты базы. Пишущие отбрасываются при
  `allow_write = False`, запрещённые отбрасываются всегда (Review Focus 4).
- `unsafe` пересчитывается: `True`, только если в итоговом наборе есть пишущий
  инструмент.

- [ ] **Step 1: Write the failing tests** (`packages/luna-core/tests/test_dynamic.py`)

```python
import pytest
from luna_core.dynamic import SubagentRequestError, resolve_spec
from luna_core.subagents import BUILTIN_SUBAGENTS, READ_ONLY, DynamicSettings, SubagentSpec
from luna_core.tools import builtin_tools
from luna_core.tools.base import tool


@tool(mutating=True)
async def mcp__db__write(sql: str) -> str:
    """Write to the db."""
    return "ok"


@tool()
async def mcp__db__query(sql: str) -> str:
    """Query the db."""
    return "rows"


@pytest.fixture
def parent_tools():
    tools = {t.name: t for t in builtin_tools()}
    for extra in (mcp__db__write, mcp__db__query):
        tools[extra.name] = extra
    tools["task"] = object()  # the parent has it; a child must never get it
    return tools


def resolve(args, parent_tools, *, base="researcher", skills=("sql",), **settings):
    return resolve_spec(
        BUILTIN_SUBAGENTS[base],
        args,
        parent_tools=parent_tools,
        skills=skills,
        settings=DynamicSettings(**settings),
    )


def test_plain_call_returns_the_base_unchanged(parent_tools):
    spec, dynamic = resolve({"description": "x"}, parent_tools)
    assert spec is BUILTIN_SUBAGENTS["researcher"] and dynamic is False


def test_max_steps_alone_is_not_dynamic(parent_tools):
    spec, dynamic = resolve({"max_steps": 5}, parent_tools)
    assert spec.max_steps == 5 and dynamic is False


def test_fields_override_the_base(parent_tools):
    spec, dynamic = resolve(
        {
            "prompt": "You audit SQL.",
            "tools": ["read_file", "mcp__db__query"],
            "skills": ["sql"],
            "model": "fast",
            "context": "postgres 16",
            "max_steps": 12,
        },
        parent_tools,
    )
    assert dynamic is True
    assert spec.prompt == "You audit SQL."
    assert spec.tools == ("read_file", "mcp__db__query")
    assert (spec.skills, spec.model, spec.context, spec.max_steps) == (
        ("sql",), "fast", "postgres 16", 12,
    )
    assert spec.name == "researcher" and not spec.unsafe


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"tools": ["read_file", "task"]}, "cannot get"),
        ({"tools": ["teleport"]}, "unknown tools"),
        ({"tools": ["edit_file"]}, "allow_write"),
        ({"tools": ["mcp__db__write"]}, "allow_write"),
        ({"tools": []}, "at least one tool"),
        ({"tools": "read_file"}, "list of tool names"),
        ({"prompt": "   "}, "prompt must be a non-empty string"),
        ({"skills": ["nope"]}, "unknown skills"),
        ({"model": "gpt-9"}, "not allowed"),
        ({"max_steps": 31}, "between 1 and 30"),
        ({"max_steps": True}, "between 1 and 30"),
        ({"context": 5}, "context must be a string"),
    ],
)
def test_bad_requests_are_explained(parent_tools, args, message):
    with pytest.raises(SubagentRequestError, match=message):
        resolve(args, parent_tools)


def test_write_tools_need_allow_write(parent_tools):
    spec, _ = resolve({"tools": ["read_file", "edit_file"]}, parent_tools, allow_write=True)
    assert spec.tools == ("read_file", "edit_file") and spec.unsafe and spec.writes


def test_allowed_model_ids_come_from_settings(parent_tools):
    spec, _ = resolve({"model": "my-model-id"}, parent_tools, models=("my-model-id",))
    assert spec.model == "my-model-id"


def test_dynamic_off_rejects_dynamic_fields_but_not_plain_calls(parent_tools):
    with pytest.raises(SubagentRequestError, match="disabled"):
        resolve({"prompt": "p"}, parent_tools, enabled=False)
    assert resolve({"description": "x"}, parent_tools, enabled=False)[1] is False


def test_writer_base_is_reduced_to_reading_without_allow_write(parent_tools):
    spec, dynamic = resolve({"prompt": "Summarise."}, parent_tools, base="general-purpose")
    assert dynamic and set(spec.tools) == {*READ_ONLY, "write_todos"}
    assert not spec.unsafe and not spec.writes


def test_writer_base_keeps_its_tools_with_allow_write(parent_tools):
    spec, _ = resolve(
        {"prompt": "Fix it."}, parent_tools, base="general-purpose", allow_write=True
    )
    assert "edit_file" in spec.tools and spec.unsafe


def test_duplicate_tools_are_collapsed_in_order(parent_tools):
    spec, _ = resolve({"tools": ["grep", "read_file", "grep"]}, parent_tools)
    assert spec.tools == ("grep", "read_file")


def test_custom_base_is_kept_as_the_name(parent_tools):
    base = SubagentSpec("db", "db work", "p", ("mcp__db__query",))
    spec, _ = resolve_spec(
        base, {"context": "c"}, parent_tools=parent_tools, skills=(), settings=DynamicSettings()
    )
    assert spec.name == "db" and spec.tools == ("mcp__db__query",)
```

В `test_public_api.py` к множеству `EXPECTED` добавить `"DynamicSettings", "resolve_spec"`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_dynamic.py packages/luna-core/tests/test_public_api.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'luna_core.dynamic'`).

- [ ] **Step 3: Implement** `packages/luna-core/src/luna_core/dynamic.py`

```python
"""Dynamic subagents: build a :class:`SubagentSpec` from the fields of a ``task`` call."""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping

from luna_core.errors import LunaCoreError
from luna_core.roles import ROLES
from luna_core.subagents import CHILD_FORBIDDEN, WRITE_TOOLS, DynamicSettings, SubagentSpec

DYNAMIC_FIELDS = ("prompt", "tools", "skills", "model", "context")


class SubagentRequestError(LunaCoreError):
    """The parent asked for a subagent it may not have; the message is for the model."""


def is_writing(name: str, parent_tools: Mapping[str, object]) -> bool:
    """True for tools that change files, run commands or are marked mutating."""
    return name in WRITE_TOOLS or bool(getattr(parent_tools.get(name), "mutating", False))


def _present(value: object) -> bool:
    return value is not None and value != "" and value != []


def _string_list(args: Mapping[str, object], key: str, what: str) -> tuple[str, ...]:
    value = args[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SubagentRequestError(f"{key} must be a list of {what}, got {value!r}")
    return tuple(dict.fromkeys(value))


def resolve_spec(
    base: SubagentSpec,
    args: Mapping[str, object],
    *,
    parent_tools: Mapping[str, object],
    skills: Collection[str],
    settings: DynamicSettings,
) -> tuple[SubagentSpec, bool]:
    """Return the spec the call asks for and whether it is a dynamic assembly.

    Raises :class:`SubagentRequestError` with a message the parent model can act on.
    """
    dynamic = any(key in args and _present(args[key]) for key in DYNAMIC_FIELDS)
    # An explicit empty list is a request too: it must fail, not fall back silently.
    if args.get("tools") == []:
        raise SubagentRequestError("tools must name at least one tool")
    changes: dict[str, object] = {}
    if "max_steps" in args and args["max_steps"] is not None:
        steps = args["max_steps"]
        if isinstance(steps, bool) or not isinstance(steps, int) or not (
            1 <= steps <= settings.max_steps
        ):
            raise SubagentRequestError(
                f"max_steps must be an integer between 1 and {settings.max_steps}, got {steps!r}"
            )
        changes["max_steps"] = steps
    if not dynamic:
        return (dataclasses.replace(base, **changes) if changes else base), False
    if not settings.enabled:
        raise SubagentRequestError(
            "dynamic subagents are disabled ([subagents] dynamic = false); "
            "call task with subagent_type only"
        )

    if "prompt" in args and args["prompt"] is not None:
        prompt = args["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise SubagentRequestError("prompt must be a non-empty string")
        changes["prompt"] = prompt.strip()

    if "tools" in args and args["tools"] is not None:
        if not isinstance(args["tools"], list):
            raise SubagentRequestError(
                f"tools must be a list of tool names, got {args['tools']!r}"
            )
        names = _string_list(args, "tools", "tool names")
        forbidden = sorted(set(names) & CHILD_FORBIDDEN)
        if forbidden:
            raise SubagentRequestError(
                f"a subagent cannot get {forbidden}: subagents do not delegate "
                "or change extensions"
            )
        unknown = sorted(set(names) - set(parent_tools))
        if unknown:
            raise SubagentRequestError(
                f"unknown tools {unknown}; a subagent can get only your own tools"
            )
        writes = sorted(n for n in names if is_writing(n, parent_tools))
        if writes and not settings.allow_write:
            raise SubagentRequestError(
                f"{writes} change files or run commands; dynamic subagents are read-only "
                "unless the user sets [subagents] allow_write = true"
            )
    else:
        names = tuple(
            n
            for n in base.tools
            if n not in CHILD_FORBIDDEN
            and (settings.allow_write or not is_writing(n, parent_tools))
        )
    changes["tools"] = names
    changes["unsafe"] = any(is_writing(n, parent_tools) for n in names)

    if "skills" in args and args["skills"] is not None:
        wanted = _string_list(args, "skills", "skill names")
        unknown = sorted(set(wanted) - set(skills))
        if unknown:
            raise SubagentRequestError(
                f"unknown skills {unknown}; available: {', '.join(sorted(skills)) or 'none'}"
            )
        changes["skills"] = wanted

    if "model" in args and args["model"] is not None:
        model = args["model"]
        if not isinstance(model, str) or (model not in ROLES and model not in settings.models):
            allowed = [*ROLES, *settings.models]
            raise SubagentRequestError(
                f"model {model!r} is not allowed; use one of: {', '.join(allowed)}"
            )
        changes["model"] = model

    if "context" in args and args["context"] is not None:
        if not isinstance(args["context"], str):
            raise SubagentRequestError("context must be a string")
        changes["context"] = args["context"].strip()

    return dataclasses.replace(base, **changes), True
```

Порядок проверки `tools`: сначала `tools == []` (выше), затем тип не-список
(сообщение «list of tool names»), затем элементы. `_string_list` для не-списка
сюда уже не доходит.

В `luna_core/__init__.py` добавить импорт
`from luna_core.dynamic import resolve_spec` и
`DynamicSettings` в импорт из `luna_core.subagents`; оба имени — в `__all__`
(в алфавитном порядке, как соседи).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/luna-core/tests/test_dynamic.py packages/luna-core/tests/test_public_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/dynamic.py packages/luna-core/src/luna_core/__init__.py \
  packages/luna-core/tests/test_dynamic.py packages/luna-core/tests/test_public_api.py
git commit -m "feat(subagents): resolve_spec builds a dynamic subagent from task fields"
```

---

### Task 3: `SubagentRunner` — запуск готовой сборки

**Files:**
- Modify: `packages/luna-core/src/luna_core/subagents.py` (`SubagentResult`
  ~335–361, `SubagentRunner` ~364–550)
- Test: `packages/luna-core/tests/test_subagents.py`

**Interfaces:**
- Consumes: Task 1 (`SubagentSpec`, `DynamicSettings`, `CHILD_FORBIDDEN`).
- Produces:
  - `SubagentRunner` получает поля:
    - `dynamic: DynamicSettings = field(default_factory=DynamicSettings)`;
    - `skill_loader: Callable[[str], str] | None = None` — текст скила по имени;
    - `skill_names: Callable[[], Collection[str]] | None = None` — какие скилы есть.
  - `SubagentRunner.run(parent, session, spec: str | SubagentSpec, task, *, isolation="auto", emit=None, cancel=None, turn_id=None, dynamic=False, resume=None) -> SubagentResult`.
    `resume` реализуется в Task 4; здесь он только принимается.
  - `SubagentResult` получает поля с умолчаниями: `steps: int = 0`,
    `cost: float = 0.0`, `hit_max_steps: bool = False`, `cancelled: bool = False`,
    `dynamic: bool = False`.
  - Метаданные дочерней сессии: `{"kind": "subagent", "agent": name, "dynamic": bool, "spec": spec.to_dict()}`.
  - Модель: если `spec.model` — имя роли из `ROLES`, берётся `parent.roles.get(role, …)`.
    Иначе это ID модели у провайдера родителя. Без модели — роль `fast`, как сейчас.
  - Системный промпт ребёнка: `spec.prompt`, затем
    `# Context from the parent agent` (если `context`), затем
    `# Skill: <name>` с текстом каждого скила.

- [ ] **Step 1: Write the failing tests** (в `test_subagents.py`)

```python
async def test_runner_accepts_a_ready_spec_and_builds_the_prompt(tmp_path, log):
    child = FakeProvider([FakeProvider.text("audited")])
    spec = SubagentSpec(
        "researcher", "d", "You audit SQL.", ("read_file", "task"),
        skills=("sql",), context="postgres 16",
    )
    runner = SubagentRunner(
        {}, model_for=lambda s: (child, "m"), skill_loader=lambda n: f"BODY OF {n}"
    )
    parent = parent_agent(tmp_path, [], child)
    session = Session.create(log, str(tmp_path), undo=False)
    result = await runner.run(parent, session, spec, "check queries", dynamic=True)
    assert result.ok and result.text == "audited" and result.dynamic
    request = child.requests[0]
    assert request.system.startswith("You audit SQL.")
    assert "# Context from the parent agent\n\npostgres 16" in request.system
    assert "# Skill: sql\n\nBODY OF sql" in request.system
    # ``task`` is never given to a child, even when a spec lists it
    assert [t.name for t in request.tools] == ["read_file"]
    meta = log.children(session.id)[0].meta
    assert meta["dynamic"] is True and meta["spec"]["prompt"] == "You audit SQL."
    assert result.steps == 1 and result.cost >= 0 and not result.hit_max_steps


async def test_unknown_profile_name_still_reports_the_choices(tmp_path, log):
    runner = SubagentRunner(dict(BUILTIN_SUBAGENTS))
    parent = parent_agent(tmp_path, [], FakeProvider([]))
    session = Session.create(log, str(tmp_path), undo=False)
    result = await runner.run(parent, session, "nope", "x")
    assert not result.ok and "available: researcher" in result.error


async def test_missing_skill_loader_or_skill_is_an_error(tmp_path, log):
    parent = parent_agent(tmp_path, [], FakeProvider([]))
    session = Session.create(log, str(tmp_path), undo=False)
    spec = SubagentSpec("r", "d", "p", skills=("sql",))
    result = await SubagentRunner({}).run(parent, session, spec, "x")
    assert not result.ok and "skills are not available" in result.error

    def boom(name):
        raise LookupError(f"no skill named {name!r}")

    result = await SubagentRunner({}, skill_loader=boom).run(parent, session, spec, "x")
    assert not result.ok and "no skill named 'sql'" in result.error


async def test_profile_tool_missing_in_this_host_is_a_warning(tmp_path, log):
    child = FakeProvider([FakeProvider.text("ok")])
    spec = SubagentSpec("r", "d", "p", ("read_file", "mcp__gone__x"))
    runner = SubagentRunner({}, model_for=lambda s: (child, "m"))
    parent = parent_agent(tmp_path, [], child)
    session = Session.create(log, str(tmp_path), undo=False)
    seen = []
    result = await runner.run(parent, session, spec, "x", emit=seen.append)
    assert result.ok
    assert any(
        isinstance(e, Notice) and e.level == "warn" and "mcp__gone__x" in e.text for e in seen
    )


async def test_role_name_as_model_uses_the_role(tmp_path, log):
    from luna_core.roles import ModelRoles

    fast = FakeProvider([FakeProvider.text("from plan role")])
    parent = parent_agent(tmp_path, [], FakeProvider([]))
    parent.roles = ModelRoles(plan=(fast, "plan-model"))
    session = Session.create(log, str(tmp_path), undo=False)
    spec = SubagentSpec("r", "d", "p", model="plan")
    result = await SubagentRunner({}).run(parent, session, spec, "x")
    assert result.text == "from plan role" and fast.requests[0].model == "plan-model"


async def test_hitting_max_steps_is_reported(tmp_path, log):
    child = FakeProvider([FakeProvider.call("ls", {}), FakeProvider.text("never")])
    spec = SubagentSpec("r", "d", "p", ("ls",), max_steps=1)
    runner = SubagentRunner({}, model_for=lambda s: (child, "m"))
    parent = parent_agent(tmp_path, [], child)
    session = Session.create(log, str(tmp_path), undo=False)
    result = await runner.run(parent, session, spec, "x")
    assert result.hit_max_steps and result.steps == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_subagents.py -q -k "ready_spec or choices or skill_loader or missing_in_this_host or role_name or max_steps_is"`
Expected: FAIL (`TypeError: ... unexpected keyword argument 'skill_loader'`).

- [ ] **Step 3: Implement**

`SubagentResult` — добавить поля в конец (у всех есть умолчания):

```python
    steps: int = 0
    cost: float = 0.0
    hit_max_steps: bool = False
    cancelled: bool = False
    dynamic: bool = False
```

Новая функция модуля (рядом с `SubagentResult`):

```python
def child_prompt(spec: SubagentSpec, skill_bodies: list[tuple[str, str]]) -> str:
    """The subagent's own part of its system prompt: role, parent context, skills."""
    parts = [spec.prompt.rstrip()]
    if spec.context.strip():
        parts.append(f"# Context from the parent agent\n\n{spec.context.strip()}")
    parts += [f"# Skill: {name}\n\n{body.strip()}" for name, body in skill_bodies]
    return "\n\n".join(parts)


@dataclass
class _Outcome:
    ok: bool
    error: str
    text: str
    child_id: str
    steps: int
    cost: float
    hit_max_steps: bool
    cancelled: bool
```

`SubagentRunner` — новые поля после `model_for`:

```python
    dynamic: DynamicSettings = field(default_factory=DynamicSettings)
    skill_loader: Callable[[str], str] | None = None
    skill_names: Callable[[], Collection[str]] | None = None
```

`_model`:

```python
    def _model(self, parent, spec: SubagentSpec) -> tuple[object, str]:
        if self.model_for is not None:
            return self.model_for(spec)
        roles = getattr(parent, "roles", None)
        if spec.model in ROLES:
            if roles is not None:
                return roles.get(spec.model, parent.provider, parent.model)
            return parent.provider, parent.model
        if spec.model:
            return parent.provider, spec.model
        if roles is not None:
            return roles.get("fast", parent.provider, parent.model)
        return parent.provider, parent.model
```

(импорт `from luna_core.roles import ROLES` вверху модуля).

Скилы загружаются до занятия слота:

```python
    def _skill_bodies(self, spec: SubagentSpec) -> list[tuple[str, str]]:
        if not spec.skills:
            return []
        if self.skill_loader is None:
            raise LunaCoreError("skills are not available in this host")
        return [(name, self.skill_loader(name)) for name in spec.skills]
```

Начало `run` — новая сигнатура и разбор `spec`:

```python
    async def run(
        self,
        parent,
        session: Session,
        spec: str | SubagentSpec,
        task: str,
        *,
        isolation: str = "auto",
        emit: Callable[[object], None] | None = None,
        cancel: asyncio.Event | None = None,
        turn_id: str | None = None,
        dynamic: bool = False,
        resume: str | None = None,
    ) -> SubagentResult:
        """Run subagent ``spec`` (a ready spec or a profile name) on ``task``."""
        if isinstance(spec, str):
            found = self.specs.get(spec)
            if found is None:
                return SubagentResult(
                    spec, False, "", f"unknown subagent; available: {', '.join(self.specs)}"
                )
            spec = found
        name = spec.name
        try:
            skill_bodies = self._skill_bodies(spec)
        except Exception as exc:  # noqa: BLE001 - any loader failure is the parent's to see
            return SubagentResult(name, False, "", str(exc), dynamic=dynamic)
        ...  # дальше — существующий код run без изменений до вызова _drive
```

Вызов `_drive` и сборка результата:

```python
                out = await self._drive(
                    parent, session, spec, task, workspace, use_worktree, emit, cancel,
                    turn_id, skill_bodies, dynamic,
                )
            ...
            ok, error = out.ok, out.error
            ...  # блок finish_worktree без изменений (ok, error переопределяются при ошибке)
            return SubagentResult(
                name, ok, out.text, error, branch, base, diffstat, out.child_id,
                steps=out.steps, cost=out.cost, hit_max_steps=out.hit_max_steps,
                cancelled=out.cancelled, dynamic=dynamic,
            )
```

`_drive` — новые параметры `skill_bodies: list[tuple[str, str]]`, `dynamic: bool`,
возвращает `_Outcome`. Изменения внутри:

```python
        wanted = [t for t in spec.tools if t not in CHILD_FORBIDDEN]
        missing = [t for t in wanted if t not in parent.tools]
        if missing:
            emit(Notice("warn", f"{name}: tools not available here: {', '.join(missing)}"))
        child = Agent(
            provider,
            model,
            tools=[parent.tools[t] for t in wanted if t in parent.tools],
            system=build_system_prompt(
                child_prompt(spec, skill_bodies), root=workspace.root, memory=[]
            ),
            ...  # остальные аргументы без изменений
        )
        child_id = session.log.create(
            str(workspace.root),
            title=f"{name}: {task[:60]}",
            parent_id=session.id,
            meta={
                "kind": "subagent",
                "agent": name,
                "dynamic": dynamic,
                "spec": spec.to_dict(),
            },
        )
```

В цикле событий добавить разбор `TurnDone` и `Cancelled`:

```python
        hit_max_steps = cancelled = False
        ...
                    elif isinstance(event, TurnDone):
                        hit_max_steps = event.stop_reason == "max_steps"
                    elif isinstance(event, Cancelled):
                        ok, error, cancelled = False, "cancelled", True
```

(импорт `TurnDone` из `luna_core.events`).

После `finally`:

```python
        messages = child_session.messages()
        answers = [m for m in messages if m.role == "assistant" and m.text]
        steps = sum(1 for m in messages if m.role == "assistant")
        return _Outcome(
            ok, error, answers[-1].text if answers else "", child_id,
            steps, child_session.cost(), hit_max_steps, cancelled,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/luna-core/tests -q`
Expected: PASS (весь пакет ядра, включая `test_background.py` и `test_acp.py`).

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/subagents.py packages/luna-core/tests/test_subagents.py
git commit -m "feat(subagents): runner takes a ready spec, adds context/skills to the prompt, reports steps and cost"
```

---

### Task 4: `resume` — продолжение прошлого субагента

**Files:**
- Modify: `packages/luna-core/src/luna_core/subagents.py` (`create_worktree`/
  `_create_worktree`, `SubagentResult.render`, `SubagentRunner.run`/`_drive`)
- Test: `packages/luna-core/tests/test_subagents.py`

**Interfaces:**
- Consumes: Task 3 (`run(..., resume=)`, метаданные `spec`/`dynamic`, `_Outcome`).
- Produces:
  - `create_worktree(root, sub_id, *, branch: str | None = None, base: str | None = None)`.
    С `branch`, который существует, worktree открывается на этой ветке без `-b`, и
    возвращается `(path, branch, base)`.
  - После `finish_worktree` с непустым diff в метаданные ребёнка пишутся `branch`
    и `base` (`log.set_meta`).
  - `run(..., resume=child_id)`:
    1. `log.info(child_id)` должен существовать, иметь `parent_id == session.id` и
       `meta["kind"] == "subagent"`. Иначе ошибка
       `no subagent session '<id>' of this session to resume`.
    2. Сессия, которая сейчас выполняется, даёт ошибку
       `subagent session '<id>' is still running`.
    3. Сборка берётся из `meta["spec"]`, если есть, иначе профиль
       `meta["agent"]`; `dynamic` — из `meta`. Аргумент `spec` вызова
       игнорируется.
    4. Новое сообщение `task` дописывается в ту же дочернюю сессию
       (`Session.open(log, child_id, undo=False)`).
    5. При worktree-изоляции и `meta["branch"]`, если ветка ещё есть: worktree
       открывается на ней, `base = meta["base"]`. Тогда diff и подсказка
       `git apply` охватывают всю работу, а не только последнюю часть. Если ветки
       нет — новый worktree как обычно.
    6. `steps` и `cost` результата — только за этот запуск.
  - `SubagentResult.render()` при непустом `session_id` дописывает строку
    `[subagent session <id>; pass it as resume to continue this subagent]`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_resume_continues_the_same_conversation(tmp_path, log):
    child = FakeProvider([FakeProvider.text("first"), FakeProvider.text("second")])
    runner = SubagentRunner(dict(BUILTIN_SUBAGENTS), model_for=lambda s: (child, "m"))
    parent = parent_agent(tmp_path, [], child)
    session = Session.create(log, str(tmp_path), undo=False)
    spec = SubagentSpec("researcher", "d", "Custom role.", ("ls",))
    one = await runner.run(parent, session, spec, "part one", dynamic=True)
    assert "pass it as resume" in one.render() and one.session_id in one.render()
    two = await runner.run(parent, session, "reviewer", "part two", resume=one.session_id)
    assert two.ok and two.text == "second" and two.session_id == one.session_id
    assert two.dynamic and two.steps == 1
    # the resumed turn saw the first exchange and kept the original assembly
    request = child.requests[1]
    assert [m.text for m in request.messages if m.role == "user"] == ["part one", "part two"]
    assert request.system.startswith("Custom role.")
    assert len(log.children(session.id)) == 1


async def test_resume_refuses_foreign_and_unknown_sessions(tmp_path, log):
    child = FakeProvider([FakeProvider.text("a")])
    runner = SubagentRunner(dict(BUILTIN_SUBAGENTS), model_for=lambda s: (child, "m"))
    parent = parent_agent(tmp_path, [], child)
    mine = Session.create(log, str(tmp_path), undo=False)
    other = Session.create(log, str(tmp_path), undo=False)
    done = await runner.run(parent, other, "researcher", "x")
    for bad in (done.session_id, other.id, "nope"):
        result = await runner.run(parent, mine, "researcher", "y", resume=bad)
        assert not result.ok and "no subagent session" in result.error


async def test_resume_of_a_running_subagent_is_refused(tmp_path, log):
    gate = asyncio.Event()

    @tool()
    async def wait() -> str:
        """Block until released."""
        await gate.wait()
        return "released"

    child = FakeProvider([FakeProvider.call("wait", {}), FakeProvider.text("done")])
    runner = SubagentRunner({}, model_for=lambda s: (child, "m"))
    parent = parent_agent(tmp_path, [], child, tools=[*builtin_tools(), wait])
    session = Session.create(log, str(tmp_path), undo=False)
    spec = SubagentSpec("w", "d", "p", ("wait",))
    first = asyncio.create_task(runner.run(parent, session, spec, "go"))
    for _ in range(100):
        if log.children(session.id):
            break
        await asyncio.sleep(0.01)
    child_id = log.children(session.id)[0].id
    refused = await runner.run(parent, session, spec, "again", resume=child_id)
    assert not refused.ok and "still running" in refused.error
    gate.set()
    assert (await first).ok


async def test_resume_in_a_worktree_continues_the_branch(tmp_path, log):
    init_repo(tmp_path)
    child = FakeProvider(
        [
            FakeProvider.call("write_file", {"file_path": "a.txt", "content": "one\n"}),
            FakeProvider.text("wrote a"),
            FakeProvider.call("write_file", {"file_path": "b.txt", "content": "two\n"}),
            FakeProvider.text("wrote b"),
        ]
    )
    runner = SubagentRunner(dict(BUILTIN_SUBAGENTS), model_for=lambda s: (child, "m"))
    parent = parent_agent(tmp_path, [], child)
    session = Session.create(log, str(tmp_path), undo=False)
    one = await runner.run(parent, session, "general-purpose", "a", isolation="worktree")
    two = await runner.run(
        parent, session, "general-purpose", "b", isolation="worktree", resume=one.session_id
    )
    assert two.branch == one.branch and two.base == one.base
    assert "a.txt" in two.diffstat and "b.txt" in two.diffstat
    assert not (tmp_path / "a.txt").exists()  # the user's tree is untouched
```

`init_repo` — уже существующий помощник в этом файле.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_subagents.py -q -k resume`
Expected: FAIL (resume ещё не реализован).

- [ ] **Step 3: Implement**

`create_worktree` / `_create_worktree`:

```python
def create_worktree(
    root: Path, sub_id: str, *, branch: str | None = None, base: str | None = None
) -> tuple[Path, str, str]:
    """Create a worktree on a new branch from the current working tree (uncommitted edits too).

    With an existing ``branch`` (a resumed subagent) the worktree checks that branch
    out instead and ``base`` is returned unchanged, so diffs cover all of its work.
    The base snapshot includes untracked, non-ignored files (``.luna/undo`` and
    ``.luna/worktrees`` excluded). ``root`` must be the git top level.
    """
    with _worktree_lock(root):
        return _create_worktree(root, sub_id, branch=branch, base=base)


def _create_worktree(
    root: Path, sub_id: str, *, branch: str | None = None, base: str | None = None
) -> tuple[Path, str, str]:
    _prune_worktrees(root)
    path = root / ".luna" / "worktrees" / sub_id
    if branch and base and _git(root, "rev-parse", "--verify", "-q", branch).returncode == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        added = _git(root, "worktree", "add", "-q", str(path), branch)
        if added.returncode != 0:
            raise LunaCoreError(f"git worktree add failed: {added.stderr.strip()}")
        return path, branch, base
    ...  # существующий код создания от снимка, без изменений
```

(строку `path = ...` из старого кода убрать, она теперь выше.)

`SubagentRunner` — множество запущенных дочерних сессий:

```python
    _running: set[str] = field(default_factory=set, init=False, repr=False)
```

В `run` сразу после разбора `spec` (до загрузки скилов):

```python
        resumed: SessionInfo | None = None
        if resume:
            resumed = session.log.info(resume)
            if (
                resumed is None
                or resumed.parent_id != session.id
                or resumed.meta.get("kind") != "subagent"
            ):
                return SubagentResult(
                    name, False, "", f"no subagent session {resume!r} of this session to resume"
                )
            if resume in self._running:
                return SubagentResult(
                    name, False, "", f"subagent session {resume!r} is still running"
                )
            stored = resumed.meta.get("spec")
            spec = (
                SubagentSpec.from_dict(stored)
                if stored
                else self.specs.get(resumed.meta.get("agent", ""), spec)
            )
            name = spec.name
            dynamic = bool(resumed.meta.get("dynamic"))
```

(импорт `SessionInfo` из `luna_core.session`.)

Создание worktree с учётом прошлой ветки:

```python
                    created = asyncio.ensure_future(
                        asyncio.to_thread(
                            create_worktree,
                            root,
                            sub_id,
                            branch=resumed.meta.get("branch") if resumed else None,
                            base=resumed.meta.get("base") if resumed else None,
                        )
                    )
```

Передать `resume_id=resume` в `_drive`. Внутри `_drive` заменить создание дочерней
сессии:

```python
        if resume_id:
            child_id = resume_id
            child_session = Session.open(session.log, child_id, undo=False)
        else:
            child_id = session.log.create(...)  # как в Task 3
            child_session = Session(session.log, child_id, workdir=str(workspace.root), undo=False)
        before = child_session.messages()
        cost_before = child_session.cost()
        self._running.add(child_id)
        session.adopt(child_session)
```

В `finally` добавить `self._running.discard(child_id)`. Итог:

```python
        messages = child_session.messages()[len(before):]
        answers = [m for m in messages if m.role == "assistant" and m.text]
        steps = sum(1 for m in messages if m.role == "assistant")
        return _Outcome(
            ok, error, answers[-1].text if answers else "", child_id,
            steps, child_session.cost() - cost_before, hit_max_steps, cancelled,
        )
```

Гонка в тесте «still running» требует, чтобы `child_id` попал в `_running` до
первого `await` после `log.create`. Поэтому `self._running.add(child_id)` стоит
сразу за созданием или открытием сессии.

В `run` после `finish_worktree`, когда `diffstat` непуст:

```python
                if diffstat:
                    branch, base = worktree[1], worktree[2]
                    session.log.set_meta(out.child_id, branch=branch, base=base)
```

`SubagentResult.render` — в конец перед `return head`:

```python
        if self.session_id:
            head += (
                f"\n\n[subagent session {self.session_id}; "
                "pass it as resume to continue this subagent]"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/luna-core/tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/subagents.py packages/luna-core/tests/test_subagents.py
git commit -m "feat(subagents): resume continues a child session (and its worktree branch)"
```

---

### Task 5: инструмент `task` — новые поля

**Files:**
- Modify: `packages/luna-core/src/luna_core/tools/tasks.py` (`task_tool`)
- Test: `packages/luna-core/tests/test_dynamic.py`

**Interfaces:**
- Consumes: Task 2 (`resolve_spec`, `SubagentRequestError`), Task 3–4
  (`runner.run(..., dynamic=, resume=)`, `runner.dynamic`, `runner.skill_names`).
- Produces: схема `task` с полями `prompt`, `tools`, `skills`, `model`,
  `max_steps`, `context`, `resume`.
  - `subagent_type` по умолчанию `general-purpose`.
  - Строка деталей результата:
    - обычный запуск: `"<name>: done|failed"`;
    - динамический: `"<name> (dyn): done|failed · <tools через запятую> · <model или fast>"`.

- [ ] **Step 1: Write the failing tests** (в `test_dynamic.py`)

```python
from luna_core.loop import Agent
from luna_core.policy import Policy
from luna_core.providers.fake import FakeProvider
from luna_core.session import EventLog, Session
from luna_core.subagents import SubagentRunner
from luna_core.tools.tasks import task_tool
from luna_core.workspace import Workspace


def agent_with(tmp_path, script, child, **runner_kw):
    runner = SubagentRunner(
        dict(BUILTIN_SUBAGENTS), model_for=lambda s: (child, "child"), **runner_kw
    )
    ws = Workspace(tmp_path)
    return Agent(
        FakeProvider(script),
        "parent",
        workspace=ws,
        tools=[*builtin_tools(), task_tool(runner)],
        policy=Policy(workspace=ws, yolo=True),
    )


async def run_turn(agent, tmp_path):
    log = EventLog(tmp_path / "db" / "s.db")
    session = Session.create(log, str(tmp_path), undo=False)
    [e async for e in agent.run(session, "go")]
    return session, log


async def test_task_call_assembles_a_dynamic_subagent(tmp_path):
    child = FakeProvider([FakeProvider.text("done it")])
    call = {"description": "audit", "prompt": "You audit.", "tools": ["grep"], "model": "fast"}
    agent = agent_with(
        tmp_path, [FakeProvider.call("task", call, "t"), FakeProvider.text("ok")], child
    )
    session, log = await run_turn(agent, tmp_path)
    result = session.messages()[2].results[0]
    assert not result.is_error and "done it" in result.text
    assert child.requests[0].system.startswith("You audit.")
    assert [t.name for t in child.requests[0].tools] == ["grep"]
    assert log.children(session.id)[0].meta["dynamic"] is True
    log.close()


async def test_task_call_with_a_forbidden_tool_is_an_error_for_the_model(tmp_path):
    call = {"description": "x", "tools": ["task"]}
    agent = agent_with(
        tmp_path, [FakeProvider.call("task", call, "t"), FakeProvider.text("ok")],
        FakeProvider([]),
    )
    session, log = await run_turn(agent, tmp_path)
    result = session.messages()[2].results[0]
    assert result.is_error and "cannot get" in result.text
    assert log.children(session.id) == []
    log.close()


async def test_task_call_resume_reaches_the_runner(tmp_path):
    child = FakeProvider([FakeProvider.text("one"), FakeProvider.text("two")])
    first = {"description": "a", "subagent_type": "researcher"}
    agent = agent_with(
        tmp_path, [FakeProvider.call("task", first, "t1"), FakeProvider.text("ok")], child
    )
    session, log = await run_turn(agent, tmp_path)
    child_id = log.children(session.id)[0].id
    agent.provider = FakeProvider(
        [FakeProvider.call("task", {"description": "b", "resume": child_id}, "t2"),
         FakeProvider.text("ok")]
    )
    [e async for e in agent.run(session, "again")]
    result = session.messages()[-2].results[0]
    assert "two" in result.text and len(log.children(session.id)) == 1
    log.close()


def test_task_schema_lists_the_new_fields():
    runner = SubagentRunner(dict(BUILTIN_SUBAGENTS))
    props = task_tool(runner).spec.parameters["properties"]
    assert {"prompt", "tools", "skills", "model", "max_steps", "context", "resume"} <= set(props)
```

Если `agent.provider` нельзя переназначить, создать второй агент через
`agent_with` с тем же `child`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/luna-core/tests/test_dynamic.py -q -k task_`
Expected: FAIL.

- [ ] **Step 3: Implement** в `tools/tasks.py`

```python
from luna_core.dynamic import SubagentRequestError, resolve_spec
from luna_core.roles import ROLES


def task_tool(runner: SubagentRunner) -> Tool:
    """Build the ``task`` tool bound to ``runner``."""

    def _assemble(parent, args: dict):
        name = str(args.get("subagent_type") or "general-purpose")
        base = runner.specs.get(name)
        if base is None:
            raise SubagentRequestError(
                f"unknown subagent_type {name!r}; available: {', '.join(runner.specs)}"
            )
        skills = runner.skill_names() if runner.skill_names else ()
        return resolve_spec(
            base, args, parent_tools=parent.tools, skills=skills, settings=runner.dynamic
        )

    def _detail(spec, dynamic: bool, ok: bool) -> str:
        status = "done" if ok else "failed"
        if not dynamic:
            return f"{spec.name}: {status}"
        return f"{spec.name} (dyn): {status} · {', '.join(spec.tools)} · {spec.model or 'fast'}"

    async def run(ctx: ToolContext, args: dict) -> ToolOutput:
        parent = ctx.services["agent"]
        session = ctx.services["session"]
        description = str(args.get("description", "")).strip()
        if not description:
            return ToolOutput.error("description is required")
        resume = str(args.get("resume") or "") or None
        try:
            spec, dynamic = (
                (runner.specs.get("general-purpose") or next(iter(runner.specs.values())), False)
                if resume
                else _assemble(parent, args)
            )
        except SubagentRequestError as exc:
            return ToolOutput.error(str(exc))
        isolation = str(args.get("isolation") or "auto")
        if args.get("background"):
            background = ctx.services.get("background")
            if background is None:
                return ToolOutput.error("background tasks are not enabled in this host")
            handle = background.start(
                f"{spec.name}: {description[:60]}",
                lambda: runner.run(
                    parent, session, spec, description, isolation=isolation,
                    emit=session.notify, dynamic=dynamic, resume=resume,
                ),
                session=session,
            )
            return ToolOutput.text(
                f"started background task {handle}; its result arrives in a later message. "
                "Check it with task_status.",
                f"background {handle}",
            )
        result = await runner.run(
            parent,
            session,
            spec,
            description,
            isolation=isolation,
            emit=ctx.emit,
            cancel=ctx.cancel,
            turn_id=ctx.services.get("turn_id"),
            dynamic=dynamic,
            resume=resume,
        )
        return ToolOutput(
            (Text(result.render()),), not result.ok, _detail(spec, result.dynamic, result.ok)
        )
```

При `resume` переданная `spec` раннером игнорируется: он берёт сборку из
метаданных, Task 4. Это значение — только заглушка для подписи фоновой задачи.
`runner.specs` никогда не пуст: встроенные профили есть всегда.

Схема — к существующим `properties` добавить:

```python
            "prompt": {
                "type": "string",
                "description": "System prompt for this subagent; replaces the profile's.",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Subset of YOUR tools to give it (MCP and LSP tools too). "
                "Never task/manage_*/save_skill.",
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Installed skills whose instructions go into its prompt.",
            },
            "model": {
                "type": "string",
                "description": "A model role or an allowed model id.",
            },
            "max_steps": {"type": "integer", "minimum": 1},
            "context": {
                "type": "string",
                "description": "Facts it needs (paths, decisions); added to its prompt.",
            },
            "resume": {
                "type": "string",
                "description": "Session id from an earlier result: continue that subagent "
                "(other fields are ignored).",
            },
```

`"default": "general-purpose"` у `subagent_type` уже есть, его не трогать.
Описание инструмента дополнить строками:

```python
    models = ", ".join([*ROLES, *runner.dynamic.models])
    description = (
        "Delegate a self-contained task to a subagent. "
        "Several task calls in one message run in parallel.\n"
        f"Subagents:\n{runner.describe()}\n"
        + (
            "You may assemble a subagent for the job: prompt, tools (a subset of yours), "
            f"skills, model ({models}), max_steps (<= {runner.dynamic.max_steps}), context. "
            + (
                "Tools that change files or run commands are allowed."
                if runner.dynamic.allow_write
                else "Assembled subagents are read-only: no tools that change files or "
                "run commands."
            )
            if runner.dynamic.enabled
            else "Only subagent_type is available (dynamic subagents are disabled)."
        )
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/luna-core/tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/luna-core/src/luna_core/tools/tasks.py packages/luna-core/tests/test_dynamic.py
git commit -m "feat(task): prompt/tools/skills/model/max_steps/context/resume fields"
```

---

### Task 6: приложение — `[subagents]`, подключение, `/agents`, README

**Files:**
- Modify: `luna/config/config.py` (`LunaConfig`, `_apply_toml`, `load_config`)
- Modify: `luna/core/agent.py:184-187` (создание `SubagentRunner`)
- Modify: `luna/commands/builtin.py` (`_agents`)
- Modify: `README.md` (раздел про субагентов, ~стр. 355)
- Test: `tests/test_config.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes: `DynamicSettings` (Task 1), поля раннера `dynamic`/`skill_loader`/
  `skill_names` (Task 3).
- Produces:
  - `LunaConfig.subagents_dynamic: bool = True`, `subagents_allow_write: bool = False`,
    `subagents_models: tuple[str, ...] = ()`, `subagents_max_steps: int = 30`.
  - Проектный `.luna.toml` может только сузить права: `allow_write = false`,
    `dynamic = false`, меньший `max_steps`. `allow_write = true` и `models` из
    проекта игнорируются с предупреждением в `config.warnings` (Review Focus 1).

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:

```python
def test_subagents_section_defaults_and_user_values(tmp_path):
    cfg = load_config({}, env={}, cwd=str(tmp_path))
    assert (cfg.subagents_dynamic, cfg.subagents_allow_write) == (True, False)
    assert (cfg.subagents_models, cfg.subagents_max_steps) == ((), 30)
    home = tmp_path / "home"
    (home / "luna").mkdir(parents=True)
    (home / "luna" / "config.toml").write_text(
        '[subagents]\ndynamic = true\nallow_write = true\nmodels = ["m-1"]\nmax_steps = 12\n'
    )
    cfg = load_config({}, env={"XDG_CONFIG_HOME": str(home)}, cwd=str(tmp_path))
    assert cfg.subagents_allow_write and cfg.subagents_models == ("m-1",)
    assert cfg.subagents_max_steps == 12


def test_project_toml_can_only_narrow_subagent_rights(tmp_path):
    (tmp_path / ".luna.toml").write_text(
        '[subagents]\nallow_write = true\nmodels = ["evil"]\nmax_steps = 99\ndynamic = false\n'
    )
    cfg = load_config({}, env={}, cwd=str(tmp_path))
    assert cfg.subagents_allow_write is False and cfg.subagents_models == ()
    assert cfg.subagents_max_steps == 30 and cfg.subagents_dynamic is False
    assert any("[subagents]" in w and "allow_write" in w for w in cfg.warnings)


@pytest.mark.parametrize(
    "body",
    ['max_steps = 0', 'max_steps = true', 'models = "m"', 'allow_write = "yes"'],
)
def test_bad_subagents_values_are_config_errors(tmp_path, body):
    home = tmp_path / "home"
    (home / "luna").mkdir(parents=True)
    (home / "luna" / "config.toml").write_text(f"[subagents]\n{body}\n")
    with pytest.raises(LunaConfigError, match=r"\[subagents\]"):
        load_config({}, env={"XDG_CONFIG_HOME": str(home)}, cwd=str(tmp_path))
```

Пользовательский `config.toml` задаётся так же, как в соседних тестах:
`env={"XDG_CONFIG_HOME": …}` → `<…>/luna/config.toml`.

`tests/test_agent.py` (рядом с тестом `caps["subagents"]`, использовать фикстуры
этого файла для конфига и `FakeProvider`):

```python
async def test_build_agent_passes_subagent_settings_and_skills(tmp_path, fake_provider):
    skill = tmp_path / ".luna" / "skills" / "sql"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: sql\ndescription: SQL help\n---\nUse CTEs.\n")
    config = LunaConfig(
        workdir=str(tmp_path), provider="anthropic", model="m",
        subagents_allow_write=True, subagents_max_steps=10,
    )
    bundle = await build_agent(config, provider=fake_provider())
    try:
        runner = bundle.agent.tools["task"].runner
        assert runner.dynamic.allow_write and runner.dynamic.max_steps == 10
        assert "sql" in runner.skill_names()
        assert runner.skill_loader("sql").strip() == "Use CTEs."
    finally:
        await bundle.aclose()
```

`Tool` сам раннер не хранит, поэтому в этой задаче `task_tool` получает атрибут
`runner` (см. Step 3). Проектные скилы лежат в `<workdir>/.luna/skills`
(`skill_dirs` в `luna/core/agent.py`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py tests/test_agent.py -q -k subagent`
Expected: FAIL (`AttributeError: 'LunaConfig' object has no attribute 'subagents_dynamic'`).

- [ ] **Step 3: Implement**

`LunaConfig` — новые поля после `max_parallel`:

```python
    subagents_dynamic: bool = True
    subagents_allow_write: bool = False
    subagents_models: tuple[str, ...] = ()
    subagents_max_steps: int = 30
```

`_apply_toml` — после блока `agent`:

```python
    subagents = data.get("subagents", {})
    if isinstance(subagents, dict):
        if project:
            # A cloned repository may narrow what assembled subagents can do, never widen it.
            widen = [
                key
                for key in ("allow_write", "models")
                if key in subagents and subagents[key] not in (False, [])
            ]
            if widen:
                into.setdefault("warnings", []).append(
                    f".luna.toml: [subagents] {', '.join(widen)} ignored: only the user "
                    "config can widen subagent rights."
                )
            if subagents.get("dynamic") is False:
                into["subagents_dynamic"] = False
            if subagents.get("allow_write") is False:
                into["subagents_allow_write"] = False
            steps = subagents.get("max_steps")
            if isinstance(steps, int) and not isinstance(steps, bool):
                into["subagents_max_steps"] = min(steps, into.get("subagents_max_steps", 30))
        else:
            for key in ("dynamic", "allow_write", "models", "max_steps"):
                if key in subagents:
                    into[f"subagents_{key}"] = subagents[key]
```

`load_config` — проверки перед `return LunaConfig(...)`:

```python
    sub_dynamic = merged.get("subagents_dynamic", True)
    sub_write = merged.get("subagents_allow_write", False)
    for key, value in (("dynamic", sub_dynamic), ("allow_write", sub_write)):
        if not isinstance(value, bool):
            raise LunaConfigError(f"[subagents] {key} must be true or false, got {value!r}.")
    sub_models = merged.get("subagents_models", ())
    if not isinstance(sub_models, (list, tuple)) or not all(
        isinstance(m, str) and m for m in sub_models
    ):
        raise LunaConfigError(
            f"[subagents] models must be a list of model ids, got {sub_models!r}."
        )
    sub_steps = merged.get("subagents_max_steps", 30)
    if isinstance(sub_steps, bool) or not isinstance(sub_steps, int) or sub_steps < 1:
        raise LunaConfigError(
            f"[subagents] max_steps must be an integer >= 1, got {sub_steps!r}."
        )
```

и в `LunaConfig(...)`:

```python
        subagents_dynamic=sub_dynamic,
        subagents_allow_write=sub_write,
        subagents_models=tuple(sub_models),
        subagents_max_steps=sub_steps,
```

Проектный слой применяется после пользовательского. Поэтому
`min(steps, into.get(...))` сравнивает с уже применённым пользовательским
значением (или 30), и проект может только уменьшить.

`luna/core/agent.py` — создание раннера:

```python
        runner = SubagentRunner(
            load_subagents(str(workdir), on_warn=on_warn),
            max_parallel=config.max_parallel,
            dynamic=DynamicSettings(
                enabled=config.subagents_dynamic,
                allow_write=config.subagents_allow_write,
                models=config.subagents_models,
                max_steps=config.subagents_max_steps,
            ),
            skill_loader=lambda name: core_skills.load_body(name, skill_dirs(workdir)),
            skill_names=lambda: [s.name for s in core_skills.discover(skill_dirs(workdir))],
        )
```

(импорт `DynamicSettings` из `luna_core.subagents` рядом с `SubagentRunner`.)

`tools/tasks.py` → в `task_tool` перед `return`:

```python
    built = Tool("task", description, schema, run, raw=True)
    built.runner = runner  # hosts and tests inspect the bound runner
    return built
```

`/agents` (`luna/commands/builtin.py`, `_agents`) — после строки с `text = ...`
добавить строку о режиме. Раннер берётся из `env.agent.tools.get("task")`:

```python
    task = env.agent.tools.get("task")
    settings = getattr(getattr(task, "runner", None), "dynamic", None)
    if settings is not None:
        mode = (
            "off"
            if not settings.enabled
            else ("may write" if settings.allow_write else "read-only")
        )
        text += f"\n\nDynamic subagents: {mode} · max_steps ≤ {settings.max_steps}"
```

README — в раздел про субагентов (после абзаца о `subagents.toml`) добавить:

```markdown
- **Динамические субагенты.** Агент может сам собрать субагента под задачу: свой
  промпт, часть своих инструментов (включая MCP и LSP), скилы, модель (роль или
  разрешённый ID), лимит шагов и контекст. Прошлого субагента можно продолжить по
  `resume`. Субагенты сами не делегируют и не меняют MCP или скилы. Настройки:

  ```toml
  [subagents]
  dynamic = true        # агент может собирать субагентов
  allow_write = false   # собранные субагенты только читают
  models = []           # разрешённые ID моделей, кроме ролей
  max_steps = 30        # потолок шагов
  ```

  `.luna.toml` проекта может эти права только сузить.
```

- [ ] **Step 4: Run the whole suite and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS, ruff без замечаний.

- [ ] **Step 5: Commit**

```bash
git add luna/config/config.py luna/core/agent.py luna/commands/builtin.py \
  packages/luna-core/src/luna_core/tools/tasks.py README.md tests/test_config.py tests/test_agent.py
git commit -m "feat: [subagents] config, skills for assembled subagents, /agents shows the mode"
```

---

## Ручная проверка после плана (не задача для исполнителя)

Не ходит в CI. Запускается вручную с реальным ключом, **1–2 запроса**, по правилу
о ключах:

```bash
uv run luna --no-splash "Собери субагента с промптом 'ты аудитор SQL' и только grep/read_file, пусть найдёт все SELECT * в проекте"
```

Ожидание: в строке инструмента видно `(dyn)` и состав; ответ приходит.
