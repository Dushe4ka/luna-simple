"""Guard: outside ``luna/ui/themes/`` colors come only from theme tokens."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "luna"
HEX = re.compile(r"[\"'\[ ]#[0-9a-fA-F]{6}\b")
NAMED = re.compile(r"(\[|style=\"|[\"'])(bold )?(red|green|yellow)\b")
#: Files still on the legacy palette, migrated in Task 5 (which empties this set).
LEGACY = {
    "ui/theme.py",
    "tui/theme.py",
    "tui/widgets.py",
    "tui/banner.py",
}


def _sources():
    for path in ROOT.rglob("*.py"):
        if "themes" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in LEGACY:
            continue
        yield path, path.read_text(encoding="utf-8")


def test_no_hex_colors_outside_themes():
    bad = [f"{p.relative_to(ROOT)}:{m.group(0)}" for p, s in _sources() for m in HEX.finditer(s)]
    assert not bad, bad


def test_no_raw_named_colors():
    bad = [f"{p.relative_to(ROOT)}:{m.group(0)}" for p, s in _sources() for m in NAMED.finditer(s)]
    assert not bad, bad


def test_no_legacy_palette_outside_tui():
    bad = [
        str(p.relative_to(ROOT))
        for p, s in _sources()
        if "tui" not in p.parts and re.search(r"\bPALETTE\b|luna\.ui\.theme import", s)
    ]
    assert not bad, bad


@pytest.mark.xfail(strict=True, reason="Task 5")
def test_tcss_uses_tokens_only():
    css = (ROOT / "tui" / "luna.tcss").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", css)
    for old in ("$moon", "$peri", "$mauve"):
        assert old not in css
