import base64
import os
import shutil
import time

import pytest
from luna_core.messages import Image
from luna_core.tools.base import ToolContext
from luna_core.tools.files import delete, edit_file, glob, grep, ls, read_file, write_file
from luna_core.workspace import Workspace


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("".join(f"line {i}\n" for i in range(1, 251)))
    (tmp_path / "src" / "b.py").write_text("def foo():\n    return 1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("def foo(): pass\n")
    return ToolContext(workspace=Workspace(tmp_path))


async def call(tool, ctx, **args):
    return await tool.invoke(ctx, args)


async def test_ls_marks_dirs_and_skips_nothing_requested(ctx):
    out = await call(ls, ctx, path=".")
    assert "src/" in out.content[0].text and not out.is_error


async def test_read_file_numbers_lines_and_pages(ctx):
    out = (await call(read_file, ctx, file_path="src/a.py")).content[0].text
    assert out.splitlines()[0] == "     1\tline 1"
    assert "line 100" in out and "line 101" not in out
    assert "150 more lines; read again with offset=100" in out
    out = (await call(read_file, ctx, file_path="/src/a.py", offset=248, limit=10)).content[0].text
    assert out.splitlines() == ["   249\tline 249", "   250\tline 250"]
    assert "src/a.py" in ctx.state["seen"]


async def test_read_image_returns_an_image_block(ctx):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    (ctx.workspace.root / "p.png").write_bytes(png)
    out = await call(read_file, ctx, file_path="p.png")
    assert isinstance(out.content[0], Image) and out.content[0].media_type == "image/png"


async def test_read_missing_and_binary_are_errors(ctx):
    assert (await call(read_file, ctx, file_path="nope.py")).is_error
    (ctx.workspace.root / "bin.dat").write_bytes(b"\x00\x01\x02" * 10)
    assert (await call(read_file, ctx, file_path="bin.dat")).is_error


async def test_write_new_file_and_refuse_blind_overwrite(ctx):
    out = await call(write_file, ctx, file_path="docs/new.md", content="# hi\n")
    assert not out.is_error and (ctx.workspace.root / "docs" / "new.md").read_text() == "# hi\n"
    blind = await call(write_file, ctx, file_path="src/b.py", content="x")
    assert blind.is_error and "read it first" in blind.content[0].text
    await call(read_file, ctx, file_path="src/b.py")
    assert not (await call(write_file, ctx, file_path="src/b.py", content="x")).is_error


async def test_write_outside_is_an_error(ctx):
    out = await call(write_file, ctx, file_path="../escape.txt", content="x")
    assert out.is_error and "outside the project" in out.content[0].text


async def test_edit_file_uniqueness_and_replace_all(ctx):
    p = ctx.workspace.root / "src" / "c.py"
    p.write_text("a = 1\na = 1\nb = 2\n")
    dup = await call(edit_file, ctx, file_path="src/c.py", old_string="a = 1", new_string="a = 3")
    assert dup.is_error and "2 times" in dup.content[0].text
    ok = await call(
        edit_file,
        ctx,
        file_path="src/c.py",
        old_string="a = 1",
        new_string="a = 3",
        replace_all=True,
    )
    assert not ok.is_error and p.read_text() == "a = 3\na = 3\nb = 2\n"
    missing = await call(edit_file, ctx, file_path="src/c.py", old_string="zzz", new_string="y")
    assert missing.is_error and "not found" in missing.content[0].text
    same = await call(edit_file, ctx, file_path="src/c.py", old_string="b = 2", new_string="b = 2")
    assert same.is_error


async def test_delete_file_dir_and_root(ctx):
    assert not (await call(delete, ctx, file_path="src/b.py")).is_error
    assert not (ctx.workspace.root / "src" / "b.py").exists()
    assert not (await call(delete, ctx, file_path="src")).is_error
    assert (await call(delete, ctx, file_path="/")).is_error


async def test_glob_newest_first_and_ignores_vendor_dirs(ctx):
    time.sleep(0.01)
    os.utime(ctx.workspace.root / "src" / "b.py")
    out = (await call(glob, ctx, pattern="**/*.py")).content[0].text.splitlines()
    assert out[0] == "src/b.py" and "src/a.py" in out
    assert not any(line.startswith("node_modules") for line in out)


@pytest.mark.parametrize("use_rg", [False, True])
async def test_grep_modes(ctx, use_rg, monkeypatch):
    if use_rg and shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    import luna_core.tools.files as files

    monkeypatch.setattr(files, "_RG", shutil.which("rg") if use_rg else None)
    names = (await call(grep, ctx, pattern=r"def foo")).content[0].text.splitlines()
    assert names == ["src/b.py"]
    content = (
        (
            await call(
                grep, ctx, pattern="line 24[0-9]", path="src", output_mode="content", max_count=2
            )
        )
        .content[0]
        .text
    )
    assert content.splitlines() == ["src/a.py:240:line 240", "src/a.py:241:line 241"]
    count = (
        (await call(grep, ctx, pattern="line", glob="*.py", output_mode="count")).content[0].text
    )
    assert "src/a.py:250" in count
    assert (await call(grep, ctx, pattern="(unclosed")).is_error
