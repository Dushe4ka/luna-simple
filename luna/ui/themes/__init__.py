"""Color themes: role tokens loaded from TOML, built into rich and Textual themes."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from luna.config.config import config_dir
from luna.ui.colors import _hex_to_rgb, _lerp_rgb, _rgb_to_hex

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
    if not user_dir.is_dir():
        return themes
    for path in sorted(user_dir.glob("*.toml")):
        theme_id = path.stem
        if theme_id in builtin:
            _warnings.append(f"тема {theme_id}: имя занято встроенной темой, файл пропущен")
            continue
        try:
            spec = _build(theme_id, _read(path), themes)
        except (ValueError, OSError) as exc:
            _warnings.append(f"тема {theme_id}: {exc} — пропущена")
            continue
        themes[theme_id] = spec if spec.ansi else _lift_contrast(spec)
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
