import base64
import datetime

from luna_core.context import PinnedFiles, build_system_prompt, expand_mentions, render_pinned
from luna_core.memory import append_note
from luna_core.messages import Image
from luna_core.skills import SkillInfo
from luna_core.workspace import Workspace


def test_prompt_sections_in_cache_friendly_order(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Use uv.")
    append_note(tmp_path, "conventions", "style", "black is banned")
    prompt = build_system_prompt(
        "BASE",
        root=tmp_path,
        skills=[SkillInfo("deploy", "ship it", tmp_path, "project")],
        extra="EXTRA",
        now=datetime.date(2026, 10, 8),
    )
    order = [
        prompt.index(s)
        for s in (
            "BASE",
            "# Environment",
            "Use uv.",
            "black is banned",
            "- deploy: ship it",
            "EXTRA",
        )
    ]
    assert order == sorted(order)
    assert str(tmp_path) in prompt and "2026-10-08" in prompt and "`skill` tool" in prompt


def test_prompt_without_optional_parts(tmp_path):
    prompt = build_system_prompt("BASE", root=tmp_path)
    assert "AGENTS.md" not in prompt and "# Skills" not in prompt and "# Memory" not in prompt


def test_mentions_attach_files_dirs_and_images(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_text("")
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    (tmp_path / "shot.png").write_bytes(png)
    text, images = expand_mentions(
        "look @a.py @d @shot.png @missing.txt @../etc/passwd", Workspace(tmp_path)
    )
    assert "<attached: a.py>\nx = 1" in text and "<dir d>\nf.txt" in text
    assert "(@missing.txt: not found)" in text and "(@../etc/passwd: outside the project)" in text
    assert (
        images == [Image("image/png", base64.b64encode(png).decode())]
        and "<image: shot.png>" in text
    )


def test_pinned_files_render_fresh(tmp_path):
    (tmp_path / "a.py").write_text("v1")
    pinned = PinnedFiles()
    pinned.add("a.py", "gone.py")
    (tmp_path / "a.py").write_text("v2")
    out = render_pinned(pinned, tmp_path)
    assert "v2" in out and "gone.py" not in out
    pinned.drop("a.py")
    assert pinned.paths == ["gone.py"]
