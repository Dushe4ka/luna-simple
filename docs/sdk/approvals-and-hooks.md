# Подтверждения и хуки

## Политика

`Policy` для каждого вызова инструмента решает: выполнить, отказать или спросить человека:

```python
from luna_core import Agent, Policy, Rules, Workspace

workspace = Workspace(".")
policy = Policy(
    Rules.load([...]),                 # список путей к TOML с таблицей [permissions]
    workspace=workspace,
    yolo=False,                        # True пропускает подтверждения (правила deny действуют)
)
agent = Agent(provider, model, workspace=workspace, policy=policy)
```

- По умолчанию `write_file`, `edit_file`, `delete` и `execute` требуют подтверждения
  (`approval_tools`); чтения выполняются свободно.
- `Rules` берутся из записей `[permissions] allow = [...] / deny = [...]`, например
  `"execute:pytest*"` или `"write:.env"`. Запрет главнее, даже `yolo`.
- `plan_mode=True` (или вызываемый объект, возвращающий bool) отклоняет каждый изменяющий вызов.
- Запись или правка файла, изменившегося на диске с момента, когда агент последний раз его читал
  или писал, отклоняется с сообщением, просящим прочитать файл заново (проверка устаревшего якоря).

## Ответы на подтверждения

Когда вызов требует решения, ход отдаёт `NeedsApproval(request)` и ждёт:

```python
from luna_core import Decision, NeedsApproval

async for event in agent.run(session, text):
    if isinstance(event, NeedsApproval):
        r = event.request                          # call_id, name, args, description, suggested_rule
        session.decide(r.call_id, Decision.approve())
        # Decision.approve(always="execute:pytest*")  также запоминает правило
        # Decision.edit({"command": "pytest -q"})      выполнить с изменёнными аргументами
        # Decision.reject("not in this repo")          модель увидит сообщение
```

Подойдёт и обычный словарь: `{"type": "approve"}`, `{"type": "edit", "args": {...}}`,
`{"type": "reject", "message": "..."}`, с необязательным `"always": "<rule>"`.
Передайте `persist_rule=fn` в `Policy`, чтобы сохранять правила «всегда» (например,
`luna_core.policy.append_rule(path, rule)` в `.luna/permissions.toml`).

Подводные камни: **неотвеченное подтверждение оставляет ход в ожидании**; `session.cancel_turn()`
(или закрытие генератора) отбрасывает всю пачку и ничего из неё не выполняет. В `run_sync` без
колбэка `approve` подтверждения отклоняются. Подтверждения субагентов идут через родительскую
сессию, поэтому отвечайте на них в той сессии, которую вы запустили.
Подтверждения по HTTP показаны в `examples/approvals_web.py`.

## Хуки

Хуки это колбэки в пяти точках: `session_start`, `pre_tool`, `post_tool`, `turn_end`,
`pre_compact`. Ключи payload (плюс `event`): у `session_start` это `session_id`, `workdir`;
у `pre_tool`: `session_id`, `tool`, `args`; `post_tool` добавляет `ok` и `output`; у `turn_end`:
`session_id`, `turn_id`, `text`; у `pre_compact`: `session_id`, `messages`. Хук `turn_end`,
вернувший `block=True` с `message`, ход не останавливает: ход продолжается, а `message`
добавляется как новое сообщение пользователя (пока позволяет `max_steps`). Блокирующий хук
`pre_compact` пропускает сжатие.

```python
from luna_core import Agent, HookResult, Hooks

hooks = Hooks()


async def no_force_push(payload: dict) -> HookResult | None:
    # payload: {"event": "pre_tool", "tool": "execute", "args": {...}, "session_id": ...}
    if "push --force" in payload["args"].get("command", ""):
        return HookResult(block=True, message="force-push is not allowed")


hooks.add("pre_tool", no_force_push, match="execute", timeout=5)
agent = Agent(provider, model, hooks=hooks)
```

Поля `HookResult`: `block` + `message` (сообщение возвращается модели), `args`
(заменяют аргументы инструмента), `context` (текст, добавляемый в диалог), `notices`.
Хуки выполняются в порядке регистрации; первый заблокировавший прерывает цепочку. Исключение
или тайм-аут превращается в уведомление и ход не блокирует.

### Командные хуки из конфигурации

```toml
[[hooks]]
event = "post_tool"
match = "write_file"      # необязательный glob по имени инструмента
command = "ruff format ."
timeout = 30
```

`luna_core.hooks.load_command_hooks(entries, cwd)` превращает такие записи в `Hooks`;
объединяйте через `hooks.extend(other)`. Команда получает JSON payload на stdin. Код выхода 0
продолжает ход (stdout может добавить контекст), код 2 блокирует (stderr уходит модели), любой
другой считается предупреждением. Тайм-аут убивает всю группу процессов.

## Отмена

`session.cancel_turn()` останавливает текущий запрос и работающие инструменты (группы процессов
убиваются), записывает `Cancelled` и исправляет повисшие вызовы инструментов, чтобы следующий
ход работал.
