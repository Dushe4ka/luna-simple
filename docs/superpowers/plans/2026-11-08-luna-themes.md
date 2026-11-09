# Темы оформления Luna — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** шесть тем по ролевым токенам (Лунный свет, Дневная луна, их варианты для дальтоников, две ANSI) + «Авто» по фону терминала, выбор при первом запуске с живым примером, `/theme`, блоки фоном и полосой слева, мерцающая звёздочка на одном таймере в цветах темы.

**Architecture:** темы — TOML-файлы в `luna/ui/themes/`; модуль `luna.ui.themes` грузит их в `ThemeSpec` и собирает из одного набора токенов тему `rich` и тему `Textual`. Фон терминала определяется один раз при запуске (`luna/ui/termbg.py`). TUI регистрирует все темы как темы Textual и переключает `App.theme` на лету; REPL меняет тему консоли `rich`. Выбор темы — inline-приложение `prompt_toolkit` с примером, отрендеренным `rich`.

**Tech Stack:** Python 3.12, rich 14, Textual 8.2.8 (`textual.theme.Theme`, `App.register_theme`, `App.theme`, markup `[$var]`), prompt_toolkit 3 (через questionary), tomllib.

**Spec:** `docs/superpowers/specs/2026-11-08-luna-themes-design.md`

## Global Constraints

- Python 3.11+, PEP 8 / PEP 257, чистый `uv run ruff check .` и `uv run ruff format --check .`.
- Тесты не ходят в сеть и не трогают реальный терминал (`/dev/tty`): определение фона тестируется через чистые функции и подмену `query`.
- Конфиг в тестах изолирован фикстурой `isolated_config_home` (`tests/conftest.py`, ставит `XDG_CONFIG_HOME`).
- Тексты интерфейса — по-русски, как в остальном TUI.
- Минимальный контраст текста встроенных не-ANSI тем: 4.5:1 к `bg`, `panel`, `element`.
- Значения `moonlight`/`daymoon` — ровно из спецификации (страница палитр); варианты для дальтоников — из таблицы спецификации (`daymoon-cb.err = #9a4806`).
- Форма анимации не меняется: TUI — `✳ ✦ ✳ ✧` (`PulseGlyph`), REPL — дышащие точки `●`.
- Вне `luna/ui/themes/` в коде `luna/` нет hex-литералов цветов и стилей `red`/`green`/`yellow`.
- Полный прогон: `uv run pytest -q` зелёный после каждой задачи.

## Review Focus

- Терминал не ответил на OSC 11 (или ответил поздно): ответ не должен попасть в поле ввода TUI. Отсюда запрос OSC 11 + DA1 и чтение до ответа DA1 — тест в задаче 3 (`test_query_reads_until_da1`).
- Светлая тема в тёмном терминале при выборе: пример должен рисоваться на `bg` темы, а не на фоне терминала — тест в задаче 6 (`test_preview_paints_theme_background`).
- Смена темы в TUI: элементы, раскрашенные в Python (значок инструмента, уведомления), должны перекраситься, а не остаться в старых цветах — тест в задаче 5 (`test_tool_mark_follows_theme`).
- Esc в выборе темы в TUI после нескольких движений курсора возвращает исходную тему — тест в задаче 7.
- Повреждённый `~/.config/luna/themes/x.toml` не роняет запуск — тест в задаче 1.

---

## Решения относительно спецификации

- `/diff` остаётся fenced-блоком ```` ```diff ```` в Markdown (подсветка идёт через тему кода `ansi_dark`/`ansi_light` по `dark` темы); раскраска токенами `add`/`del` — в окне подтверждения TUI и REPL. Причина: результат `/diff` — общий текст команды для всех клиентов, отдельный формат ради цвета не оправдан. Цена ошибки: одна правка в рендере `SystemMessage`.
- Общий таймер звёздочки в TUI работает всегда, когда анимация включена (обновление пустого списка ничего не стоит); пауза при отсутствии живых `PulseGlyph` не делается.

## Файлы

| Файл | Что |
|---|---|
| `luna/ui/themes/__init__.py` (новый) | `ThemeSpec`, загрузка, `resolve`, `rich_theme`, `textual_theme`, контраст, активная тема |
| `luna/ui/themes/{moonlight,daymoon,moonlight-cb,daymoon-cb,ansi-dark,ansi-light}.toml` (новые) | темы |
| `luna/ui/termbg.py` (новый) | фон терминала: OSC 11 + DA1, `COLORFGBG` |
| `luna/ui/motion.py` (новый) | `breath`, `motion_enabled` |
| `luna/ui/diffview.py` (новый) | раскраска unified diff токенами |
| `luna/ui/theme_picker.py` (новый) | выбор темы с примером |
| `luna/ui/theme.py` | удаляется (`PALETTE`, `LUNA_THEME`) |
| `luna/tui/theme.py` | удаляется (`TUI_VARIABLES`) |
| `luna/ui/console.py` | тема консоли из активной темы, `use_theme` |
| `luna/config/config.py` | `ui.theme`, `ui.motion`, `LUNA_THEME`, `user_setting` |
| `luna/cli.py` | определение фона, первый запуск, применение темы |
| `luna/repl/setup_wizard.py` | шаг «Тема» |
| `luna/commands/builtin.py` | `/theme` |
| `luna/repl/commands.py` | `pick_theme` для эффекта, применение `theme` |
| `luna/tui/app.py`, `luna/tui/luna.tcss`, `luna/tui/chat.py`, `luna/tui/tool_row.py`, `luna/tui/widgets.py`, `luna/tui/pickers.py`, `luna/tui/banner.py`, `luna/tui/approval_modal.py`, `luna/tui/sidebar_sessions.py` | переход на токены, блоки, `ThemeModal`, общий таймер |
| `luna/ui/{approve,progress,trust,splash,turn}.py`, `luna/core/session.py`, `luna/repl/commands.py`, `luna/repl/setup_wizard.py`, `luna/cli.py` | `PALETTE` → стили `luna.*` |
| `tests/test_themes.py`, `tests/test_termbg.py`, `tests/test_motion.py`, `tests/test_theme_picker.py`, `tests/test_theme_command.py`, `tests/test_no_hex_colors.py`, `tests/test_tui_themes.py` (новые) | тесты |
| `AGENTS.md`, `README.md`, `docs/` | описание тем |

---

### Task 1: Темы как файлы и загрузчик

**Files:**
- Create: `luna/ui/themes/__init__.py`, шесть `.toml`
- Test: `tests/test_themes.py`
- Modify: `pyproject.toml` (если пакетные данные не попадают в wheel — проверить `[tool.setuptools.package-data]`/`hatch`; `*.toml` в `luna/ui/themes` должен устанавливаться)

**Interfaces:**
- Produces:
  - `TOKENS: tuple[str, ...]` — 20 имён токенов в порядке спецификации.
  - `@dataclass(frozen=True) class ThemeSpec: id: str; title: str; dark: bool; ansi: bool; tokens: Mapping[str, str]`
  - `available_themes(env: Mapping[str, str] | None = None) -> list[ThemeSpec]` — встроенные (в порядке `BUILTIN_ORDER`) + пользовательские из `config_dir(env) / "themes"`.
  - `load_theme(theme_id: str, env=None) -> ThemeSpec` — `KeyError`, если нет.
  - `theme_warnings() -> list[str]` — предупреждения последней загрузки (битые/конфликтные/подтянутый контраст).
  - `contrast(a: str, b: str) -> float`, `contrast_issues(spec) -> list[str]`.
  - `BUILTIN_ORDER = ("moonlight", "daymoon", "moonlight-cb", "daymoon-cb", "ansi-dark", "ansi-light")`
  - `AUTO = "auto"`, `AUTO_TITLE = "Авто (по фону терминала)"`.

Формат TOML:

```toml
title = "Лунный свет"
dark = true
# ansi = false        # по умолчанию
# extends = "moonlight"

[tokens]
bg = "#0e1117"
...
```

- [ ] **Step 1: тесты**

```python
# tests/test_themes.py
import pytest

from luna.ui import themes
from luna.ui.themes import BUILTIN_ORDER, TOKENS, available_themes, contrast, load_theme

READABLE = ("text", "muted", "strong", "primary", "tool", "code", "ok", "warn", "err", "add", "del")


def test_builtin_order_and_titles():
    got = [t.id for t in available_themes()]
    assert got[: len(BUILTIN_ORDER)] == list(BUILTIN_ORDER)
    assert load_theme("moonlight").title == "Лунный свет"
    assert load_theme("daymoon").title == "Дневная луна"
    assert load_theme("daymoon").dark is False
    assert load_theme("ansi-dark").ansi is True


@pytest.mark.parametrize("theme_id", BUILTIN_ORDER)
def test_builtin_defines_every_token(theme_id):
    assert set(load_theme(theme_id).tokens) == set(TOKENS)


@pytest.mark.parametrize("theme_id", [t for t in BUILTIN_ORDER if not t.startswith("ansi")])
def test_builtin_contrast(theme_id):
    tk = load_theme(theme_id).tokens
    for name in READABLE:
        for surface in ("bg", "panel", "element"):
            assert contrast(tk[name], tk[surface]) >= 4.5, (theme_id, name, surface)
    for surface in ("add_bg", "del_bg"):
        assert contrast(tk["text"], tk[surface]) >= 4.5
    assert contrast(tk["on_primary"], tk["primary"]) >= 4.5


def test_exact_spec_values():
    assert load_theme("moonlight").tokens["primary"] == "#a3b5ff"
    assert load_theme("daymoon").tokens["bg"] == "#f8f7f3"
    assert load_theme("moonlight-cb").tokens["ok"] == "#6cb8ee"
    assert load_theme("daymoon-cb").tokens["err"] == "#9a4806"


def _user_theme(tmp_path, name, body):
    d = tmp_path / ".config" / "luna" / "themes"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(body, encoding="utf-8")


def test_user_theme_extends(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _user_theme(tmp_path, "mine", 'title = "Моя"\nextends = "moonlight"\n[tokens]\nprimary = "#ffaa00"\n')
    spec = load_theme("mine")
    assert spec.tokens["primary"] == "#ffaa00"
    assert spec.tokens["bg"] == load_theme("moonlight").tokens["bg"]
    assert spec.dark is True


def test_broken_user_theme_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _user_theme(tmp_path, "bad", "title = [unclosed")
    ids = [t.id for t in available_themes()]
    assert "bad" not in ids
    assert any("bad" in w for w in themes.theme_warnings())


def test_user_theme_cannot_shadow_builtin(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _user_theme(tmp_path, "moonlight", 'title = "X"\nextends = "daymoon"\n')
    assert load_theme("moonlight").title == "Лунный свет"
    assert any("moonlight" in w for w in themes.theme_warnings())


def test_unknown_token_skips_theme(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _user_theme(tmp_path, "odd", 'title = "Odd"\nextends = "moonlight"\n[tokens]\nsparkle = "#fff000"\n')
    assert "odd" not in [t.id for t in available_themes()]


def test_low_contrast_user_theme_is_lifted(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _user_theme(tmp_path, "dim", 'title = "Dim"\nextends = "moonlight"\n[tokens]\nmuted = "#20242c"\n')
    spec = load_theme("dim")
    assert contrast(spec.tokens["muted"], spec.tokens["element"]) >= 4.5
    assert any("muted" in w for w in themes.theme_warnings())


def test_missing_theme_raises():
    with pytest.raises(KeyError):
        load_theme("nope")
```

- [ ] **Step 2:** `uv run pytest tests/test_themes.py -q` → FAIL (`ModuleNotFoundError: luna.ui.themes`).

- [ ] **Step 3: темы.** `luna/ui/themes/moonlight.toml`:

```toml
title = "Лунный свет"
dark = true

[tokens]
bg = "#0e1117"
panel = "#151a23"
element = "#1e2531"
border = "#323b4b"
border_subtle = "#232a37"
text = "#e2e6ee"
muted = "#8b95a8"
strong = "#f2f4f8"
primary = "#a3b5ff"
on_primary = "#0e1117"
accent = "#e3c58f"
tool = "#c9d3ea"
code = "#c6d2ff"
ok = "#7fd3a0"
warn = "#e6b866"
err = "#f0817b"
add = "#86d6a8"
del = "#f08c86"
add_bg = "#15281f"
del_bg = "#2e1b1e"
```

`daymoon.toml` (`dark = false`): bg `#f8f7f3`, panel `#efede7`, element `#e4e1d9`, border `#cfcbc1`, border_subtle `#e2dfd7`, text `#1e2026`, muted `#5f636d`, strong `#0f1116`, primary `#4353c9`, on_primary `#ffffff`, accent `#8e4592`, tool `#2a2e3a`, code `#3b46a8`, ok `#1d7442`, warn `#87590a`, err `#bf3a33`, add `#1d7442`, del `#b8352e`, add_bg `#dcefe0`, del_bg `#f6dcd8`.

`moonlight-cb.toml`: `title = "Лунный свет · для дальтоников"`, `extends = "moonlight"`, tokens: ok/add `#6cb8ee`, err/del `#f08a4b`, warn `#e8d36a`, add_bg `#142536`, del_bg `#33200f`.

`daymoon-cb.toml`: `title = "Дневная луна · для дальтоников"`, `extends = "daymoon"`, ok/add `#0a62a3`, err/del `#9a4806`, warn `#6f5c00`, add_bg `#dbe9f6`, del_bg `#f8e3d2`.

`ansi-dark.toml`: `title = "Тёмная · цвета терминала"`, `dark = true`, `ansi = true`; bg/panel/element/add_bg/del_bg `"default"`, text `"default"`, border/border_subtle/muted `"ansi_bright_black"`, strong `"ansi_bright_white"`, primary `"ansi_bright_blue"`, on_primary `"ansi_black"`, accent `"ansi_magenta"`, tool `"ansi_cyan"`, code `"ansi_bright_cyan"`, ok/add `"ansi_green"`, warn `"ansi_yellow"`, err/del `"ansi_red"`.

`ansi-light.toml`: `title = "Светлая · цвета терминала"`, `dark = false`, `ansi = true`; как `ansi-dark`, но strong `"ansi_black"`, primary `"ansi_blue"`, on_primary `"ansi_white"`, code `"ansi_blue"`.

- [ ] **Step 4: модуль.**

```python
"""Color themes: role tokens loaded from TOML, built into rich and Textual themes."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from luna.config.config import config_dir

TOKENS: tuple[str, ...] = (
    "bg", "panel", "element", "border", "border_subtle", "text", "muted", "strong",
    "primary", "on_primary", "accent", "tool", "code", "ok", "warn", "err",
    "add", "del", "add_bg", "del_bg",
)
#: Text tokens the contrast guard checks against every surface.
READABLE = ("text", "muted", "strong", "primary", "tool", "code", "ok", "warn", "err", "add", "del")
SURFACES = ("bg", "panel", "element")
MIN_CONTRAST = 4.5
BUILTIN_ORDER = ("moonlight", "daymoon", "moonlight-cb", "daymoon-cb", "ansi-dark", "ansi-light")
AUTO = "auto"
AUTO_TITLE = "Авто (по фону терминала)"
_BUILTIN_DIR = Path(__file__).parent
_warnings: list[str] = []


@dataclass(frozen=True)
class ThemeSpec:
    """One resolved theme: every token set, plus its metadata."""

    id: str
    title: str
    dark: bool
    ansi: bool
    tokens: Mapping[str, str]
```

Остальное в модуле:
- `_read(path) -> dict` (tomllib; ошибка → `ValueError` с текстом).
- `_build(theme_id, raw, base: dict[str, ThemeSpec]) -> ThemeSpec`: `extends` берёт токены, `dark`, `ansi` базы; неизвестные ключи в `[tokens]` → `ValueError("unknown token 'x'")`; после слияния все `TOKENS` обязаны быть заданы, иначе `ValueError`.
- `_load_all(env) -> dict[str, ThemeSpec]`: сначала встроенные в порядке `BUILTIN_ORDER` (без контрастной правки, ошибки во встроенных — исключение, это баг пакета), затем `sorted(glob("*.toml"))` из `config_dir(env) / "themes"`: id совпадает со встроенным → предупреждение `f"тема {id}: имя занято встроенной темой, файл пропущен"`; любая `ValueError`/`OSError` → предупреждение `f"тема {id}: {exc} — пропущена"`; для не-ANSI пользовательских — `_lift_contrast`.
- `_lift_contrast(spec) -> ThemeSpec`: для каждого `READABLE` токена, пока `min(contrast к SURFACES) < 4.5`, смешивать с белым (тёмная тема) или чёрным (светлая) шагом 5% (не более 20 шагов); каждый изменённый токен → предупреждение `f"тема {id}: {token} подтянут до контраста 4.5:1"`.
- `contrast(a, b)` — WCAG по относительной яркости (тот же расчёт, что в `luna/ui/colors.py`; если там есть `_hex_to_rgb` — использовать).
- `contrast_issues(spec)` — список `"{token}/{surface} {ratio:.2f}"` ниже порога (для ANSI — пустой).
- `available_themes(env=None)` → `list(_load_all(env).values())`; `load_theme(id, env=None)` → `_load_all(env)[id]` (`KeyError` наружу); `theme_warnings()` → копия `_warnings` (очищается в начале `_load_all`).
- Токены хранить как `MappingProxyType`.

- [ ] **Step 5:** `uv run pytest tests/test_themes.py -q` → PASS. Проверить, что `.toml` попадают в установку: `uv run python -c "import luna.ui.themes as t; print(t.load_theme('ansi-light').title)"`.

- [ ] **Step 6: коммит** `feat(themes): role-token themes as TOML files with loader and contrast guard`.

---

### Task 2: Сборка тем rich/Textual и активная тема

**Files:**
- Modify: `luna/ui/themes/__init__.py`
- Test: `tests/test_themes.py` (дописать)

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `resolve(choice: str, *, dark_terminal: bool, env=None) -> ThemeSpec` — `"auto"` → `moonlight`/`daymoon`; неизвестное → как `auto` + предупреждение в `theme_warnings()` `f"неизвестная тема {choice!r} — используется «Авто»"`.
  - `rich_color(value: str) -> str` — `"ansi_bright_blue"` → `"bright_blue"`, `"default"` → `"default"`, hex как есть.
  - `rich_theme(spec) -> rich.theme.Theme`
  - `textual_name(spec) -> str` → `f"luna-{spec.id}"`
  - `textual_theme(spec) -> textual.theme.Theme`
  - `active() -> ThemeSpec` (по умолчанию `moonlight`), `set_active(spec) -> None`.
  - Стили rich: `luna.<token>` для каждого токена, имя как есть (`luna.border_subtle`, `luna.add_bg`); плюс смысловые: `luna.title` = `bold {strong}`, `luna.prompt` = `bold {primary}`, `luna.tagline` = `italic {accent}`, `luna.step` = `{primary}`, `luna.tool.name` = `bold {tool}`, `luna.ok` = `{ok}`, `luna.warn` = `{warn}`, `luna.error` = `{err}`, `luna.diff.add` = `{add} on {add_bg}`, `luna.diff.del` = `{del} on {del_bg}`, `luna.diff.ctx` = `{muted}`, `luna.diff.hunk` = `{primary}`.
  - Переменные Textual: все токены через дефис (`border-subtle`, `on-primary`, `add-bg`, `del-bg`) **кроме** `text` (его роль — стандартный `$foreground`); `ansi_*`/`default` → `ansi_*`/`ansi_default`.

- [ ] **Step 1: тесты**

```python
from rich.style import Style
from luna.ui.themes import active, resolve, rich_theme, set_active, textual_theme, theme_warnings


def test_resolve_auto():
    assert resolve("auto", dark_terminal=True).id == "moonlight"
    assert resolve("auto", dark_terminal=False).id == "daymoon"
    assert resolve("daymoon-cb", dark_terminal=True).id == "daymoon-cb"


def test_resolve_unknown_falls_back_to_auto():
    assert resolve("neon", dark_terminal=False).id == "daymoon"
    assert any("neon" in w for w in theme_warnings())


def test_rich_theme_styles():
    styles = rich_theme(load_theme("moonlight")).styles
    assert styles["luna.primary"] == Style.parse("#a3b5ff")
    assert styles["luna.error"] == Style.parse("#f0817b")
    assert styles["luna.diff.add"] == Style.parse("#86d6a8 on #15281f")


def test_rich_theme_ansi_names():
    styles = rich_theme(load_theme("ansi-dark")).styles
    assert styles["luna.primary"] == Style.parse("bright_blue")


def test_textual_theme_variables():
    t = textual_theme(load_theme("daymoon"))
    assert t.name == "luna-daymoon" and t.dark is False
    assert t.background == "#f8f7f3" and t.foreground == "#1e2026"
    assert t.variables["muted"] == "#5f636d"
    assert t.variables["add-bg"] == "#dcefe0"
    assert "text" not in t.variables


def test_textual_theme_ansi():
    t = textual_theme(load_theme("ansi-light"))
    assert t.ansi is True and t.variables["primary"] == "ansi_blue"
    assert t.background == "ansi_default"


def test_active_roundtrip():
    before = active()
    try:
        set_active(load_theme("daymoon"))
        assert active().id == "daymoon"
    finally:
        set_active(before)
```

- [ ] **Step 2:** запуск → FAIL (`ImportError: resolve`).
- [ ] **Step 3: реализация.** `textual_theme`:

```python
def textual_theme(spec: ThemeSpec) -> TextualTheme:
    tk = {k: _textual_color(v) for k, v in spec.tokens.items()}
    variables = {k.replace("_", "-"): v for k, v in tk.items() if k != "text"}
    return TextualTheme(
        name=textual_name(spec),
        primary=tk["primary"], secondary=tk["accent"], accent=tk["accent"],
        background=tk["bg"], surface=tk["panel"], panel=tk["panel"],
        foreground=tk["text"], success=tk["ok"], warning=tk["warn"], error=tk["err"],
        dark=spec.dark, ansi=spec.ansi, variables=variables,
    )
```

`_textual_color`: `"default"` → `"ansi_default"`, иначе как есть. Импорт `textual.theme` — внутри функции (REPL и `luna --version` не тянут Textual).

- [ ] **Step 4:** PASS. **Step 5: коммит** `feat(themes): build rich and Textual themes from tokens; active theme`.

---

### Task 3: Фон терминала и настройки `ui.theme` / `ui.motion`

**Files:**
- Create: `luna/ui/termbg.py`, `tests/test_termbg.py`
- Modify: `luna/config/config.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `parse_osc11(reply: str) -> bool | None` — `True` = тёмный; `None` — не разобрать.
  - `parse_colorfgbg(value: str | None) -> bool | None`.
  - `query_osc11(timeout: float = 0.2) -> str | None` — пишет `"\x1b]11;?\x07\x1b[c"` в `/dev/tty`, читает до ответа DA1 (`\x1b[?…c`) или таймаута; без TTY/`termios` → `None`.
  - `detect_dark(env: Mapping[str, str] | None = None, *, query=query_osc11) -> bool` — OSC 11 → `COLORFGBG` → `True`; результат кэшируется на процесс (`functools.cache` нельзя из-за `env` — модульная переменная `_cached`; `reset_cache()` для тестов).
  - `LunaConfig.theme: str = "auto"`, `LunaConfig.motion: bool = True`; `_SETTABLE` += `"ui.theme": "str"`, `"ui.motion": "bool"`; `LUNA_THEME` в окружении → `theme`.
  - `user_setting(dotted: str, env=None) -> object | None` — значение ключа только из пользовательского `config.toml` (для решения «спрашивать ли тему»).

- [ ] **Step 1: тесты**

```python
# tests/test_termbg.py
from luna.ui import termbg


def test_parse_osc11_dark_and_light():
    assert termbg.parse_osc11("\x1b]11;rgb:0e0e/1111/1717\x07") is True
    assert termbg.parse_osc11("\x1b]11;rgb:f8f8/f7f7/f3f3\x1b\\") is False
    assert termbg.parse_osc11("\x1b]11;rgb:ff/ff/ff\x07\x1b[?62;22c") is False


def test_parse_osc11_garbage():
    assert termbg.parse_osc11("") is None
    assert termbg.parse_osc11("\x1b[?62c") is None


def test_parse_colorfgbg():
    assert termbg.parse_colorfgbg("15;0") is True
    assert termbg.parse_colorfgbg("0;15") is False
    assert termbg.parse_colorfgbg("0;default;7") is False
    assert termbg.parse_colorfgbg(None) is None
    assert termbg.parse_colorfgbg("x") is None


def test_detect_prefers_osc(monkeypatch):
    termbg.reset_cache()
    assert termbg.detect_dark({"COLORFGBG": "15;0"}, query=lambda: "\x1b]11;rgb:ffff/ffff/ffff\x07") is False


def test_detect_falls_back(monkeypatch):
    termbg.reset_cache()
    assert termbg.detect_dark({"COLORFGBG": "0;15"}, query=lambda: None) is False
    termbg.reset_cache()
    assert termbg.detect_dark({}, query=lambda: None) is True


def test_query_without_tty_returns_none(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert termbg.query_osc11() is None
```

Плюс тест `test_query_reads_until_da1`: подменить `os.open/os.read/os.write/select.select/termios/tty` через `monkeypatch` на фейковый fd, отдающий ответ двумя кусками (`"\x1b]11;rgb:0/0/0\x07"`, затем `"\x1b[?62;22c"`), и проверить, что функция вернула строку, содержащую оба, и что `tcsetattr` вызван (терминал восстановлен).

```python
# tests/test_config.py — дописать
def test_ui_theme_and_motion(tmp_path, monkeypatch):
    from luna.config.config import config_path, load_config, set_config_values, user_setting
    assert load_config().theme == "auto" and load_config().motion is True
    assert user_setting("ui.theme") is None
    set_config_values({"ui.theme": "daymoon", "ui.motion": "false"})
    cfg = load_config()
    assert cfg.theme == "daymoon" and cfg.motion is False
    assert user_setting("ui.theme") == "daymoon"


def test_luna_theme_env(monkeypatch):
    from luna.config.config import load_config
    monkeypatch.setenv("LUNA_THEME", "ansi-dark")
    assert load_config().theme == "ansi-dark"
```

- [ ] **Step 2:** FAIL. **Step 3:** реализация. `parse_osc11`: regex `rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)`, каждое значение `int(h, 16) / (16 ** len(h) - 1)`; яркость `0.2126 r + 0.7152 g + 0.0722 b` (без гаммы — достаточно для порога) `< 0.5` → тёмный. `parse_colorfgbg`: последнее поле — фон; `int` в `{0..6, 8}` → тёмный, `{7, 15}` → светлый, иначе `None`. `query_osc11`: `try: import termios, tty` иначе `None`; `sys.stdin.isatty() and sys.stdout.isatty()` иначе `None`; `os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)`; `tty.setraw`; цикл `select` до `re.search(rb"\x1b\[\?[\d;]*c", buf)` или дедлайна; `finally` — `tcsetattr(fd, termios.TCSADRAIN, old)` и `os.close`. Конфиг: в `_apply_toml` `if "theme" in ui: into["theme"] = str(ui["theme"])`, `if "motion" in ui: into["motion"] = bool(ui["motion"])`; в `_apply_env` `LUNA_THEME`; поля и сборка в `load_config`; `user_setting` читает `_load_toml(config_path(env))`.
- [ ] **Step 4:** PASS. **Step 5: коммит** `feat(themes): detect terminal background; ui.theme / ui.motion settings`.

---

### Task 4: REPL и общий rich-слой на токенах

**Files:**
- Create: `luna/ui/motion.py`, `luna/ui/diffview.py`, `tests/test_motion.py`, `tests/test_no_hex_colors.py`
- Modify: `luna/ui/console.py`, `luna/ui/approve.py`, `luna/ui/progress.py`, `luna/ui/trust.py`, `luna/ui/splash.py`, `luna/ui/turn.py`, `luna/core/session.py`, `luna/repl/commands.py`, `luna/repl/setup_wizard.py`, `luna/cli.py`, тесты, ссылающиеся на `PALETTE`
- Delete: `luna/ui/theme.py`

**Interfaces:**
- Consumes: `active`, `rich_theme`, `set_active` (Task 2).
- Produces:
  - `luna.ui.motion.breath(elapsed: float, period: float, low: str, high: str, *, floor: float = 0.0) -> str` — hex-смесь `low→high` по синусу; если любой цвет не hex → `high`.
  - `luna.ui.motion.motion_enabled(env=None, *, setting: bool = True) -> bool` — `False` при `TEXTUAL_ANIMATIONS=none`, непустом `NO_MOTION` или `setting=False`.
  - `luna.ui.console.use_theme(spec: ThemeSpec) -> None` — `set_active(spec)` + замена темы у консоли (`pop_theme` предыдущей нашей, `push_theme(rich_theme(spec))`).
  - `luna.ui.diffview.render_diff(text: str) -> rich.text.Text` — строки `+`/`-`/`@@`/контекст со стилями `luna.diff.*` (заголовки `+++`/`---` — `luna.muted`).

- [ ] **Step 1: тесты**

```python
# tests/test_motion.py
from luna.ui.motion import breath, motion_enabled


def test_breath_endpoints():
    assert breath(0.0, 4.0, "#000000", "#ffffff") in {"#7f7f7f", "#808080"}
    assert breath(1.0, 4.0, "#000000", "#ffffff") == "#ffffff"


def test_breath_ansi_is_static():
    assert breath(0.3, 4.0, "ansi_blue", "ansi_bright_blue") == "ansi_bright_blue"


def test_motion_enabled():
    assert motion_enabled({}) is True
    assert motion_enabled({"TEXTUAL_ANIMATIONS": "none"}) is False
    assert motion_enabled({"NO_MOTION": "1"}) is False
    assert motion_enabled({}, setting=False) is False
```

```python
# tests/test_no_hex_colors.py
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "luna"
HEX = re.compile(r"[\"'\[ ]#[0-9a-fA-F]{6}\b")
NAMED = re.compile(r"(\[|style=\"|[\"'])(bold )?(red|green|yellow)\b")


def _sources():
    for path in ROOT.rglob("*.py"):
        if "themes" in path.parts:
            continue
        yield path, path.read_text(encoding="utf-8")


def test_no_hex_colors_outside_themes():
    bad = [f"{p.relative_to(ROOT)}:{m.group(0)}" for p, s in _sources() for m in HEX.finditer(s)]
    assert not bad, bad


def test_no_raw_named_colors():
    bad = [f"{p.relative_to(ROOT)}:{m.group(0)}" for p, s in _sources() for m in NAMED.finditer(s)]
    assert not bad, bad


def test_tcss_uses_tokens_only():
    css = (ROOT / "tui" / "luna.tcss").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", css)
    for old in ("$moon", "$peri", "$mauve"):
        assert old not in css
```

Тест `test_tcss_uses_tokens_only` будет красным до Task 5 — пометить `@pytest.mark.xfail(strict=True, reason="Task 5")` и снять пометку в Task 5.

Плюс `tests/test_approve.py`: `test_edit_diff_uses_theme_styles` — `_panel` для `edit_file` содержит `Text` со спаном стиля `luna.diff.add`.

- [ ] **Step 2:** FAIL. **Step 3: реализация и миграция.**
  - `console.py`: `Console(theme=rich_theme(active()), ...)`; `use_theme` как в Interfaces (хранить флаг «наша тема на стеке»).
  - `approve.py`: окно подтверждения без рамки, со сплошной полосой `┃` слева в `luna.warn`: `Table.grid(padding=(0, 1))` из двух колонок — левая `Text("┃\n" * высота, style="luna.warn")` не годится для переноса строк, поэтому использовать `Panel(..., box=LEFT_BAR, border_style="luna.warn")`, где `LEFT_BAR = box.Box("    \n┃   \n┃   \n┃   \n┃   \n┃   \n┃   \n    \n")` (верх/низ пустые, левая сторона `┃`); заголовок `f"[luna.tool.name]{action}[/] wants to run"`; `edit_file` → `render_diff(diff)`, `execute` → `Syntax(..., theme="ansi_dark" if active().dark else "ansi_light")`; `PALETTE["mauve"]` → `"luna.warn"`.
  - `progress.py`: `_pulse_color` → `breath(clock * _PULSE_SPEED + dot * _PULSE_DOT_OFFSET, 2π, tk["muted"], tk["primary"], floor=0.35)` с `tk = active().tokens` (сохранить нынешнюю форму яркости `0.35 + 0.65·…`); ok/err-итог: `"luna.ok"`/`"luna.error"` + значок `✓`/`✗` перед итогом вместо цвета-only; label `"luna.tool.name"`, время/деталь `"luna.muted"`; без анимации (`motion_enabled(setting=…)` — настройку передать из вызывающего кода через параметр `motion: bool = True` конструктора) — статичный `•`.
  - `trust.py`, `turn.py`, `splash.py`, `session.py`, `repl/commands.py`, `setup_wizard.py`, `cli.py`: `PALETTE['peri']` → `luna.primary`, `moon` → `luna.text`, `moon_dim` → `luna.muted`, `blue` → `luna.muted` (текст) / `luna.border` (линии), `mauve` → `luna.error` там, где это ошибка, и `luna.warn` там, где предупреждение, `accent` → `luna.strong`; `[yellow]` → `[luna.warn]`, `[green]` → `[luna.ok]`, `"dim"` оставить (это не цвет). Карта уведомлений `session.py:_STYLE`/`repl/commands.py:_STYLE`: `dim/ok → "luna.muted"`, `info → "luna.primary"`, `warn → "luna.warn"`, `error → "luna.error"`.
  - `splash.py`: градиент `[primary, strong, accent, primary]` из `active().tokens`; если тема ANSI — сплошной `luna.primary` (через `luna.ui.colors` интерполяция только для hex).
  - Удалить `luna/ui/theme.py`; поправить импорты в тестах (`tests/test_splash.py`, `tests/test_progress.py` и др. — `grep -rn "luna.ui.theme\b\|PALETTE" tests`).
- [ ] **Step 4:** `uv run pytest -q` → всё зелёное (кроме `xfail` tcss). **Step 5: коммит** `refactor(ui): rich surfaces read colors from the active theme`.

---

### Task 5: TUI на темах Textual, блоки и общий таймер

**Files:**
- Modify: `luna/tui/app.py`, `luna/tui/luna.tcss`, `luna/tui/chat.py`, `luna/tui/tool_row.py`, `luna/tui/widgets.py`, `luna/tui/banner.py`, `luna/tui/approval_modal.py`, `luna/tui/sidebar_sessions.py`, `luna/tui/status_bar.py` (если берёт цвета), `luna/cli.py` (`run_tui`)
- Delete: `luna/tui/theme.py`
- Create: `tests/test_tui_themes.py`; поправить `tests/test_tui_*.py`, ссылающиеся на `TUI_VARIABLES`

**Interfaces:**
- Consumes: `available_themes`, `resolve`, `textual_theme`, `textual_name`, `set_active`, `render_diff`, `breath`.
- Produces:
  - `LunaApp.__init__(..., theme_choice: str = "auto", dark_terminal: bool = True, motion: bool = True)`; в `__init__` регистрирует `textual_theme(t)` для каждой `available_themes()` и вызывает `apply_theme_choice(theme_choice)`.
  - `LunaApp.apply_theme_choice(choice: str) -> ThemeSpec` — `resolve` → `self.theme = textual_name(spec)` → `set_active(spec)`.
  - `LunaApp.theme_choice: str` — текущий выбор (`auto` или id).
  - Общий таймер: `on_mount` → `self.set_interval(GLYPH_SECONDS, self._pulse)`, если `self.animation_level != "none"`; `_pulse` вызывает `refresh()` у всех `PulseGlyph`. `PulseGlyph` больше не ставит `auto_refresh`.
  - `motion=False` → `self.animation_level = "none"`.
  - CSS-классы `ToolRow.-running/-ok/-err`, `NoticeRow.-dim/-info/-warn/-error`.
  - `run_tui(config, *, workdir, thread_id, dark_terminal: bool)` передаёт `theme_choice=config.theme`, `motion=config.motion`.

Переименование переменных в `luna.tcss`: `$moon` → `$foreground`, `$moon-dim` → `$muted`, `$peri` → `$primary`, `$mauve` → `$accent`; `$bg`, `$panel`, `$border`, `$ok`, `$warn`, `$err` — те же имена. Убрать `get_theme_variable_defaults`.

Блоки (`luna.tcss`):

```css
UserMessage { background: $panel; border-left: outer $primary; padding: 0 1; margin: 1 1 1 1; }
ToolRow { background: $panel; border-left: outer $muted; padding: 0 1; margin: 0 1 1 3; }
ToolRow.-ok { border-left: outer $ok; }
ToolRow.-err { border-left: outer $err; }
NoticeRow.-warn { border-left: outer $warn; color: $warn; padding-left: 1; }
NoticeRow.-error { border-left: outer $err; color: $err; padding-left: 1; }
NoticeRow.-info { color: $primary; }
NoticeRow.-dim { color: $muted; }
ApprovalModal > Vertical { background: $element; border: none; border-left: outer $warn; }
ChoiceModal > Vertical, ConfirmModal > Vertical, KeyModal > Vertical {
    background: $panel; border: none; border-left: outer $primary;
}
SessionsSidebar ListItem.session-item.-current { background: $element; border-left: outer $primary; }
SessionsSidebar ListItem.session-item.-current Label { color: $foreground; }
ChatPane.-plan #chat-input, ChatPane.-plan #chat-input:focus { border: round $accent; }
```

`outer` vs `heavy` — сравнить в pty (Task 8); если `outer` (`▌`) смотрится тяжело, заменить на `heavy` (`┃`) одной правкой и записать решение в ledger. `#autocomplete` — фон `$panel`, без рамки, полоса `$border`.

- [ ] **Step 1: тесты**

```python
# tests/test_tui_themes.py
import pytest
from textual.color import Color

from tests.tui_fakes import make_app  # существующая фабрика; при отсутствии — собрать LunaApp как в test_tui_app.py


@pytest.mark.asyncio
async def test_theme_switch_changes_background():
    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        pane = app.query_one("ChatPane")
        assert pane.styles.background == Color.parse("#0e1117")
        app.apply_theme_choice("daymoon")
        await pilot.pause()
        assert pane.styles.background == Color.parse("#f8f7f3")


@pytest.mark.asyncio
async def test_auto_uses_terminal_background():
    app = make_app(theme_choice="auto", dark_terminal=False)
    async with app.run_test():
        assert app.theme == "luna-daymoon"


@pytest.mark.asyncio
async def test_tool_mark_follows_theme():
    from luna.tui.tool_row import ToolRow
    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        row = ToolRow("read_file", "a.py", result=(True, "ok"))
        await app.query_one("#transcript").mount(row)
        await pilot.pause()
        assert row.has_class("-ok")
        before = str(row.query_one(".tool-head").render())
        app.apply_theme_choice("moonlight-cb")
        await pilot.pause()
        head = row.query_one(".tool-head")
        assert "✓" in str(head.render())
        # the mark's colour is a theme variable, so its rendered segment style follows the theme
        segs = list(head.render_line(0))  # Strip
        assert any(s.style and s.style.color and s.style.color.triplet.hex == "#6cb8ee" for s in segs)


@pytest.mark.asyncio
async def test_single_pulse_timer(monkeypatch):
    from luna.tui.widgets import PulseGlyph
    app = make_app(theme_choice="moonlight")
    async with app.run_test() as pilot:
        await app.query_one("#transcript").mount(PulseGlyph("a"), PulseGlyph("b"))
        await pilot.pause()
        assert all(g.auto_refresh is None for g in app.query(PulseGlyph))


@pytest.mark.asyncio
async def test_motion_off():
    app = make_app(theme_choice="moonlight", motion=False)
    async with app.run_test():
        assert app.animation_level == "none"
```

(Точную форму проверки цвета значка в `test_tool_mark_follows_theme` подогнать под фактический API `Static`/`Strip` Textual 8.2 — суть: цвет `✓` равен `ok` новой темы.)

- [ ] **Step 2:** FAIL. **Step 3: реализация.**
  - `tool_row.py`: заголовок — `Static(Content.from_markup(f"[$ok]✓[/] [b]{escape(name)}[/]  [$muted]«{escape(preview)}»[/]"))` (`✗` и `$err` при ошибке; `from textual.markup import escape`), класс `-ok`/`-err` на `ToolRow`, `-running` пока идёт.
  - `chat.py`: `_user_line` — без инлайн-цвета (`›` в `[$primary]`), `NoticeRow` — `Static(Text(text))` + `add_class(f"-{level}")`; убрать `_NOTICE_COLOURS`.
  - `widgets.py`: цвет кадра — `breath(elapsed, _PULSE_SECONDS, vars["primary"], vars["accent"])`, где `vars = self.app.theme_variables`; убрать `auto_refresh`.
  - `banner.py`: `_LIT`/`_SHADOW` → `theme_variables["strong"]`/`["border"]` в момент рендера; подписи — `[$primary]`/`[$accent]`.
  - `approval_modal.py`: для `edit_file` тело — `render_diff(...)` (стили `luna.diff.*` здесь не определены → в TUI отдать `Text` с явными цветами из `app.theme_variables`: добавить в `diffview` параметр `palette: Mapping[str, str] | None` — `None` → стили `luna.diff.*`, иначе цвета из словаря).
  - `cli.py:run_tui` — новые аргументы.
  - Удалить `luna/tui/theme.py`; снять `xfail` с `test_tcss_uses_tokens_only`.
- [ ] **Step 4:** `uv run pytest -q` зелёный. **Step 5: коммит** `feat(tui): Textual themes from tokens, background blocks, one pulse timer`.

---

### Task 6: Выбор темы с примером и первый запуск

**Files:**
- Create: `luna/ui/theme_picker.py`, `tests/test_theme_picker.py`
- Modify: `luna/cli.py`, `luna/repl/setup_wizard.py`, `tests/test_cli.py` / `tests/test_startup.py`, `tests/test_setup_wizard.py`

**Interfaces:**
- Consumes: `available_themes`, `resolve`, `AUTO`, `AUTO_TITLE`, `user_setting`, `set_config_values`, `detect_dark`, `use_theme`.
- Produces:
  - `theme_options(dark_terminal: bool) -> list[tuple[str, str]]` — `[("auto", f"{AUTO_TITLE} — сейчас: {resolved.title}"), *((t.id, t.title) for t in available_themes())]`.
  - `render_preview(spec: ThemeSpec, width: int = 60) -> str` — ANSI-строка примера (своё сообщение, строка инструмента с `✓`, diff из 4 строк, строка ответа с `code`), все строки залиты `bg` темы на всю ширину.
  - `pick_theme(*, current: str, dark_terminal: bool, input=None, output=None) -> str | None` — `prompt_toolkit` `Application(full_screen=False)`; ↑/↓ двигают курсор и перерисовывают пример; Enter → id; Esc → `"auto"`; Ctrl+C → `KeyboardInterrupt`. `input`/`output` — для тестов (`create_pipe_input`, `DummyOutput`).
  - `luna.cli._first_run_theme(console, config, dark: bool) -> None` — спрашивает, если `user_setting("ui.theme") is None` и нет `LUNA_THEME`; сохраняет; Esc печатает `тема: Авто · сменить — /theme`.

Порядок в `cli._main` после `load_config`: `dark = detect_dark()` → `if interactive and not prompt: _first_run_theme(...)` → `config = load_config(...)` (перечитать) → `use_theme(resolve(config.theme, dark_terminal=dark))` → предупреждения `theme_warnings()` печатаются `luna.warn` → далее trust-gate, заставка, `_run_tui_path(..., dark)`.

- [ ] **Step 1: тесты**

```python
# tests/test_theme_picker.py
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from luna.ui.theme_picker import pick_theme, render_preview, theme_options
from luna.ui.themes import load_theme


def _pick(keys, **kw):
    with create_pipe_input() as inp:
        inp.send_text(keys)
        with create_app_session(input=inp, output=DummyOutput()):
            return pick_theme(current="auto", dark_terminal=True, input=inp, output=DummyOutput(), **kw)


def test_enter_on_first_is_auto():
    assert _pick("\r") == "auto"


def test_arrow_down_picks_moonlight_then_daymoon():
    assert _pick("\x1b[B\r") == "moonlight"
    assert _pick("\x1b[B\x1b[B\r") == "daymoon"


def test_escape_means_auto():
    assert _pick("\x1b[B\x1b[B\x1b") == "auto"


def test_options_show_resolved_auto():
    opts = theme_options(dark_terminal=False)
    assert opts[0][0] == "auto" and "Дневная луна" in opts[0][1]


def test_preview_paints_theme_background():
    out = render_preview(load_theme("daymoon"), width=40)
    assert "48;2;248;247;243" in out          # bg #f8f7f3 as truecolor background
    assert "✓" in out and "+" in out and "-" in out
```

Esc в `prompt_toolkit` ждёт `ttimeoutlen`; в тесте поставить `app.ttimeoutlen = 0` через параметр/атрибут или отправить `"\x1b"` последним и закрыть ввод — подобрать так, чтобы тест не висел.

```python
# tests/test_startup.py (или test_cli.py) — дописать
def test_first_run_asks_once(monkeypatch):
    import luna.cli as cli
    calls = []
    monkeypatch.setattr("luna.ui.theme_picker.pick_theme", lambda **kw: calls.append(kw) or "daymoon")
    cli._first_run_theme(console=None, dark=True)
    from luna.config.config import user_setting
    assert user_setting("ui.theme") == "daymoon"
    cli._first_run_theme(console=None, dark=True)
    assert len(calls) == 1


def test_first_run_skipped_with_env(monkeypatch):
    import luna.cli as cli
    monkeypatch.setenv("LUNA_THEME", "moonlight")
    monkeypatch.setattr("luna.ui.theme_picker.pick_theme", lambda **kw: (_ for _ in ()).throw(AssertionError))
    cli._first_run_theme(console=None, dark=True)
```

Плюс тест: неинтерактивный `luna -p` (существующие тесты `test_cli.py` вызывают `_main` без TTY) не вызывает `pick_theme` — подмена, бросающая `AssertionError`.

Плюс `tests/test_setup_wizard.py`: `run_setup(..., theme_fn=lambda: "ansi-dark")` сохраняет `ui.theme`. В `run_setup` добавить параметр `theme_fn: Callable[[], str | None] | None = None` (по умолчанию — `pick_theme`, если `console.is_terminal and sys.stdin.isatty()`, иначе шаг пропускается).

- [ ] **Step 2:** FAIL. **Step 3: реализация.** `render_preview`: отдельная `Console(file=StringIO(), force_terminal=True, color_system="truecolor", width=width, theme=rich_theme(spec))`; строки — `Text` с `style=f"on {bg}"` и `pad` до ширины (`Text.align("left", width)`), полоса `┃` в `luna.primary`/`luna.ok`; diff через `render_diff`. `pick_theme`: `FormattedTextControl(lambda: ANSI(header + list + render_preview(...)))`, `KeyBindings` на `up`/`down`/`enter`/`escape`/`c-c`, `Application(layout=..., key_bindings=..., full_screen=False, erase_when_done=True, input=input, output=output)`. Для `ttimeoutlen` — `app.ttimeoutlen = 0.05`.
- [ ] **Step 4:** PASS + полный прогон. **Step 5: коммит** `feat(themes): first-run theme picker with live preview; setup step`.

---

### Task 7: Команда `/theme` в TUI и REPL

**Files:**
- Modify: `luna/commands/builtin.py`, `luna/tui/pickers.py`, `luna/tui/chat.py`, `luna/repl/commands.py`, `luna/repl/*` (справка `HELP`, если список команд ручной)
- Create: `tests/test_theme_command.py`; дописать `tests/test_tui_commands.py`

**Interfaces:**
- Consumes: Task 2, 5, 6.
- Produces:
  - `/theme` без аргумента → `CommandResult(notices=[Notice("dim", f"тема: {env.config.theme}")], effects={"pick_theme": {"current": env.config.theme}})`.
  - `/theme <id|auto>` → `set_config_values({"ui.theme": id})` в `asyncio.to_thread`, `env.config.theme = id`, `CommandResult(notices=[Notice("info", f"тема → {title}")], effects={"theme": id})`; неизвестная → `Notice("error", f"нет темы {arg!r}; есть: auto, moonlight, …")`; ошибка записи → `Notice("error", f"не удалось сохранить: {exc}")` и всё равно эффект `theme` (применить на сессию).
  - `ChoiceModal(title, options, *, initial: str | None = None)`; `ThemeModal(ChoiceModal)` с `__init__(options, *, current: str, on_preview: Callable[[str], None])`: `ListView.Highlighted` → `on_preview(value)`; Esc → `on_preview(current)` и `dismiss(None)`.
  - TUI `_render_command_result`: `pick_theme` → `ThemeModal(theme_options(self.app.dark_terminal), current=self.app.theme_choice, on_preview=self.app.apply_theme_choice)`; выбор → `_run_command(f"/theme {picked}")`; эффект `theme` → `self.app.apply_theme_choice(effects["theme"])` и `self.app.theme_choice = effects["theme"]`. Обработать до общей ветки `choice`.
  - REPL `dispatch`: эффект `pick_theme` и интерактивный ввод → `pick_theme(current=…, dark_terminal=detect_dark())` в потоке → `dispatch(f"/theme {picked}")`; эффект `theme` → `use_theme(resolve(id, dark_terminal=detect_dark()))`.

- [ ] **Step 1: тесты**

```python
# tests/test_theme_command.py
import pytest
from luna.commands import run  # фактическая точка вызова команд — как в tests/test_commands.py
from luna.config.config import user_setting


@pytest.mark.asyncio
async def test_theme_without_arg_asks(command_env):
    result = await run("/theme", command_env)
    assert result.effects["pick_theme"]["current"] == "auto"


@pytest.mark.asyncio
async def test_theme_sets_and_saves(command_env):
    result = await run("/theme daymoon", command_env)
    assert result.effects == {"theme": "daymoon"}
    assert user_setting("ui.theme") == "daymoon"
    assert "Дневная луна" in result.notices[0].text


@pytest.mark.asyncio
async def test_theme_unknown(command_env):
    result = await run("/theme neon", command_env)
    assert result.notices[0].level == "error" and "moonlight" in result.notices[0].text
    assert user_setting("ui.theme") is None
```

(`run`/`command_env` — взять существующие помощники из `tests/test_commands.py`; если фикстуры нет — завести её там же по образцу.)

TUI (`tests/test_tui_commands.py`): фейковый клиент возвращает для `/theme` эффект `pick_theme`; в модалке ↓↓ → `app.theme == "luna-daymoon"` (живой просмотр); Esc → `app.theme` снова исходная, команда `/theme …` не отправлена. Второй тест: Enter → отправлено `/theme daymoon`, после эффекта `theme` тема остаётся `luna-daymoon`.

- [ ] **Step 2:** FAIL. **Step 3:** реализация. **Step 4:** PASS + полный прогон. **Step 5: коммит** `feat(themes): /theme with live preview in the TUI`.

---

### Task 8: Документация, живая проверка, critique и polish

**Files:**
- Modify: `AGENTS.md` (структура: `luna/ui/themes/`, `termbg.py`, `motion.py`, `diffview.py`, `theme_picker.py`; убрать `luna/tui/theme.py`, `luna/ui/theme.py`), `README.md` (раздел «Темы»: список, `/theme`, `luna config set ui.theme …`, `LUNA_THEME`, свои темы в `~/.config/luna/themes/`, `ui.motion`), `docs/` где упоминается палитра.

- [ ] **Step 1:** обновить документы.
- [ ] **Step 2: живая проверка в pty 130×36** (tmux `send-keys`/`capture-pane -e`, как в прежних проверках TUI): чистый `XDG_CONFIG_HOME` → `uv run luna` показывает выбор темы; ↓ меняет пример; Enter сохраняет; запуск TUI в каждой из шести тем: сообщение, вызов инструмента (успех и ошибка), окно подтверждения с diff, выбор `/provider`, `/theme` с живым просмотром и Esc. Снимки — в scratchpad. Сравнить `outer` и `heavy` у полос, зафиксировать выбор.
- [ ] **Step 3:** `Skill critique` по снимкам, затем `Skill polish`; исправления — отдельными коммитами с тестами там, где меняется поведение.
- [ ] **Step 4:** `uv run ruff check . && uv run ruff format --check . && uv run pytest -q` зелёные.
- [ ] **Step 5: коммит** `docs(themes): document themes, /theme and custom themes`.
