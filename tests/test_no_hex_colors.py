"""Guard: outside ``luna/ui/themes/`` colors come only from theme tokens."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "luna"
#: ``#rgb`` / ``#rrggbb`` after a quote, bracket, space, paren, ``=`` or ``:``.
HEX = re.compile(r"[\"'\[ (=:]#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
#: A raw red/green/yellow style, with modifiers (``[dim red]``) or as a background (``on red``).
NAMED = re.compile(
    r"(?:\[|style=\"|[\"']|\bon )"
    r"(?:(?:bold|dim|italic|underline|reverse|not) )*"
    r"(?:bright_)?(?:red|green|yellow)\b"
)


def _sources():
    for path in ROOT.rglob("*.py"):
        if "themes" in path.parts:
            continue
        yield path, path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "sample",
    [
        "style='#abc'",
        "Style(color=#a1b2c3)",
        "color:#a1b2c3",
        '"[#a1b2c3]x"',
        "x = (#fff)",
    ],
)
def test_hex_pattern_catches(sample):
    assert HEX.search(sample)


@pytest.mark.parametrize(
    "sample",
    [
        '"[dim red]x"',
        "'bold red'",
        '"white on red"',
        '"bright_red"',
        'style="bright_green"',
        "'[italic bright_yellow]'",
        '"green"',
    ],
)
def test_named_pattern_catches(sample):
    assert NAMED.search(sample)


@pytest.mark.parametrize("sample", ["# 1. step", "see PR #12", "'redraw'", '"greenfield"'])
def test_patterns_ignore_harmless_text(sample):
    assert not HEX.search(sample) and not NAMED.search(sample)


def test_no_hex_colors_outside_themes():
    bad = [f"{p.relative_to(ROOT)}:{m.group(0)}" for p, s in _sources() for m in HEX.finditer(s)]
    assert not bad, bad


def test_no_raw_named_colors():
    bad = [f"{p.relative_to(ROOT)}:{m.group(0)}" for p, s in _sources() for m in NAMED.finditer(s)]
    assert not bad, bad


def test_no_legacy_palette():
    bad = [
        str(p.relative_to(ROOT))
        for p, s in _sources()
        if re.search(r"\bPALETTE\b|\bTUI_VARIABLES\b|luna\.(?:ui|tui)\.theme import", s)
    ]
    assert not bad, bad


def test_legacy_palette_modules_are_gone():
    assert not (ROOT / "ui" / "theme.py").exists()
    assert not (ROOT / "tui" / "theme.py").exists()


def test_tcss_uses_tokens_only():
    css = (ROOT / "tui" / "luna.tcss").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", css)
    for old in ("$moon", "$peri", "$mauve"):
        assert old not in css
