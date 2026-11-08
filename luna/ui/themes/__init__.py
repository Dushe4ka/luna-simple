"""Color themes: role tokens loaded from TOML, built into rich and Textual themes."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from luna.config.config import config_dir
from luna.ui.colors import _hex_to_rgb, _lerp_rgb, _rgb_to_hex

if TYPE_CHECKING:
    from rich.theme import Theme as RichTheme
    from textual.theme import Theme as TextualTheme

TOKENS: tuple[str, ...] = (
    "bg",
    "panel",
    "element",
    "border",
    "border_subtle",
    "text",
    "muted",
    "strong",
    "primary",
    "on_primary",
    "accent",
    "tool",
    "code",
    "ok",
    "warn",
    "err",
    "add",
    "del",
    "add_bg",
    "del_bg",
)
#: Text tokens the contrast guard checks against every surface.
READABLE = ("text", "muted", "strong", "primary", "tool", "code", "ok", "warn", "err", "add", "del")
SURFACES = ("bg", "panel", "element")
MIN_CONTRAST = 4.5
BUILTIN_ORDER = ("moonlight", "daymoon", "moonlight-cb", "daymoon-cb", "ansi-dark", "ansi-light")
AUTO = "auto"
AUTO_TITLE = "Авто (по фону терминала)"

_BUILTIN_DIR = Path(__file__).parent
_LIFT_STEP = 0.05
_LIFT_MAX_STEPS = 20
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_ANSI_RE = re.compile(r"^(default|ansi_[a-z_]+)$")
_warnings: list[str] = []


@dataclass(frozen=True)
class ThemeSpec:
    """One resolved theme: every token set, plus its metadata."""

    id: str
    title: str
    dark: bool
    ansi: bool
    tokens: Mapping[str, str]


def _read(path: Path) -> dict:
    """Parse a theme TOML file; raise ``ValueError`` with a readable message."""
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"некорректный TOML ({exc})") from exc


def _build(theme_id: str, raw: Mapping, base: Mapping[str, ThemeSpec]) -> ThemeSpec:
    """Resolve a raw theme table (with optional ``extends``) into a ``ThemeSpec``."""
    tokens: dict[str, str] = {}
    dark = bool(raw.get("dark", True))
    ansi = bool(raw.get("ansi", False))
    parent_id = raw.get("extends")
    if parent_id is not None:
        if not isinstance(parent_id, str):
            raise ValueError("extends должен быть строкой")
        parent = base.get(parent_id)
        if parent is None:
            raise ValueError(f"неизвестная база extends={parent_id!r}")
        tokens.update(parent.tokens)
        dark = bool(raw.get("dark", parent.dark))
        ansi = bool(raw.get("ansi", parent.ansi))
    own = raw.get("tokens", {})
    if not isinstance(own, Mapping):
        raise ValueError("[tokens] должен быть таблицей")
    for name, value in own.items():
        if name not in TOKENS:
            raise ValueError(f"unknown token {name!r}")
        if not isinstance(value, str):
            raise ValueError(f"токен {name!r} должен быть строкой")
        tokens[name] = value
    pattern = _ANSI_RE if ansi else _HEX_RE
    for name, value in tokens.items():
        if not pattern.match(value):
            kind = "ansi_*/default" if ansi else "#rrggbb"
            raise ValueError(f"токен {name!r}: значение {value!r} не в формате {kind}")
    missing = [name for name in TOKENS if name not in tokens]
    if missing:
        raise ValueError(f"не заданы токены: {', '.join(missing)}")
    return ThemeSpec(
        id=theme_id,
        title=str(raw.get("title", theme_id)),
        dark=dark,
        ansi=ansi,
        tokens=MappingProxyType({name: tokens[name] for name in TOKENS}),
    )


def _luminance(color: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` color."""
    channels = []
    for c in _hex_to_rgb(color):
        s = c / 255
        channels.append(s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colors (1.0 .. 21.0)."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _worst(spec_tokens: Mapping[str, str], token: str) -> float:
    return min(contrast(spec_tokens[token], spec_tokens[s]) for s in SURFACES)


def contrast_issues(spec: ThemeSpec) -> list[str]:
    """List ``token/surface ratio`` entries below the minimum (always empty for ANSI)."""
    if spec.ansi:
        return []
    issues = []
    for token in READABLE:
        for surface in SURFACES:
            ratio = contrast(spec.tokens[token], spec.tokens[surface])
            if ratio < MIN_CONTRAST:
                issues.append(f"{token}/{surface} {ratio:.2f}")
    return issues


def _lift_contrast(spec: ThemeSpec) -> ThemeSpec:
    """Blend low-contrast readable tokens toward white (dark) or black (light)."""
    target = _hex_to_rgb("#ffffff" if spec.dark else "#000000")
    tokens = dict(spec.tokens)
    for token in READABLE:
        if _worst(tokens, token) >= MIN_CONTRAST:
            continue
        start = _hex_to_rgb(tokens[token])
        for step in range(1, _LIFT_MAX_STEPS + 1):
            tokens[token] = _rgb_to_hex(_lerp_rgb(start, target, _LIFT_STEP * step))
            if _worst(tokens, token) >= MIN_CONTRAST:
                break
        _warnings.append(f"тема {spec.id}: {token} подтянут до контраста 4.5:1")
    return ThemeSpec(spec.id, spec.title, spec.dark, spec.ansi, MappingProxyType(tokens))


def _load_all(env: Mapping[str, str] | None = None) -> dict[str, ThemeSpec]:
    """Load built-in themes, then user themes from ``<config>/themes``."""
    _warnings.clear()
    themes: dict[str, ThemeSpec] = {}
    for theme_id in BUILTIN_ORDER:
        raw = _read(_BUILTIN_DIR / f"{theme_id}.toml")
        themes[theme_id] = _build(theme_id, raw, themes)
    builtin = dict(themes)
    user_dir = config_dir(env) / "themes"
    try:
        paths = sorted(user_dir.glob("*.toml")) if user_dir.is_dir() else []
    except OSError as exc:
        _warnings.append(f"темы пользователя: {exc} — пропущены")
        return themes
    for path in paths:
        theme_id = path.stem
        if theme_id in builtin:
            _warnings.append(f"тема {theme_id}: имя занято встроенной темой, файл пропущен")
            continue
        try:
            spec = _build(theme_id, _read(path), themes)
            if not spec.ansi:
                spec = _lift_contrast(spec)
        except (ValueError, OSError) as exc:
            _warnings.append(f"тема {theme_id}: {exc} — пропущена")
            continue
        themes[theme_id] = spec
    return themes


def available_themes(env: Mapping[str, str] | None = None) -> list[ThemeSpec]:
    """Built-in themes in fixed order, followed by valid user themes."""
    return list(_load_all(env).values())


def load_theme(theme_id: str, env: Mapping[str, str] | None = None) -> ThemeSpec:
    """Return one theme by id; raise ``KeyError`` if it does not exist."""
    return _load_all(env)[theme_id]


def theme_warnings() -> list[str]:
    """Warnings collected by the most recent load."""
    return list(_warnings)


def resolve(choice: str, *, dark_terminal: bool, env: Mapping[str, str] | None = None) -> ThemeSpec:
    """Pick the theme for a config choice; ``auto`` follows the terminal background."""
    themes = _load_all(env)
    auto_id = "moonlight" if dark_terminal else "daymoon"
    if choice == AUTO:
        return themes[auto_id]
    if choice in themes:
        return themes[choice]
    # Appended after _load_all, which clears the warning list at its start.
    _warnings.append(f"неизвестная тема {choice!r} — используется «Авто»")
    return themes[auto_id]


def rich_color(value: str) -> str:
    """Map a token value to a rich color: ``ansi_bright_blue`` -> ``bright_blue``."""
    if value.startswith("ansi_"):
        return value[len("ansi_") :]
    return value


def rich_theme(spec: ThemeSpec) -> RichTheme:
    """Build a ``rich`` theme with ``luna.<token>`` and semantic ``luna.*`` styles."""
    from rich.theme import Theme

    c = {name: rich_color(value) for name, value in spec.tokens.items()}
    styles = {f"luna.{name}": c[name] for name in TOKENS}
    styles.update(
        {
            "luna.title": f"bold {c['strong']}",
            "luna.prompt": f"bold {c['primary']}",
            "luna.tagline": f"italic {c['accent']}",
            "luna.step": c["primary"],
            "luna.tool.name": f"bold {c['tool']}",
            "luna.ok": c["ok"],
            "luna.warn": c["warn"],
            "luna.error": c["err"],
            "luna.diff.add": f"{c['add']} on {c['add_bg']}",
            "luna.diff.del": f"{c['del']} on {c['del_bg']}",
            "luna.diff.ctx": c["muted"],
            "luna.diff.hunk": c["primary"],
        }
    )
    return Theme(styles)


def textual_name(spec: ThemeSpec) -> str:
    """Name under which the theme is registered in Textual."""
    return f"luna-{spec.id}"


def _textual_color(value: str) -> str:
    return "ansi_default" if value == "default" else value


def textual_theme(spec: ThemeSpec) -> TextualTheme:
    """Build a Textual theme; Textual is imported lazily (REPL must not load it)."""
    from textual.theme import Theme as TextualTheme

    tk = {k: _textual_color(v) for k, v in spec.tokens.items()}
    variables = {k.replace("_", "-"): v for k, v in tk.items() if k != "text"}
    return TextualTheme(
        name=textual_name(spec),
        primary=tk["primary"],
        secondary=tk["accent"],
        accent=tk["accent"],
        background=tk["bg"],
        surface=tk["panel"],
        panel=tk["panel"],
        foreground=tk["text"],
        success=tk["ok"],
        warning=tk["warn"],
        error=tk["err"],
        dark=spec.dark,
        ansi=spec.ansi,
        variables=variables,
    )


_active: ThemeSpec | None = None


def active() -> ThemeSpec:
    """Return the theme in use (``moonlight`` until one is set)."""
    global _active
    if _active is None:
        _active = load_theme("moonlight")
    return _active


def set_active(spec: ThemeSpec) -> None:
    """Make ``spec`` the current theme."""
    global _active
    _active = spec
