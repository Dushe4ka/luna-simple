"""edit_file / write_file keep the file's CRLF/LF line endings."""

import pytest
from luna_core.tools.base import ToolContext
from luna_core.tools.files import edit_file, read_file, write_file
from luna_core.workspace import Workspace


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace=Workspace(tmp_path))


async def call(tool, ctx, **args):
    return await tool.invoke(ctx, args)


async def test_edit_keeps_crlf(ctx, tmp_path):
    (tmp_path / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    await call(read_file, ctx, file_path="w.txt")
    out = await call(edit_file, ctx, file_path="w.txt", old_string="one\ntwo", new_string="1\n2")
    assert not out.is_error
    assert (tmp_path / "w.txt").read_bytes() == b"1\r\n2\r\nthree\r\n"


async def test_edit_keeps_lf(ctx, tmp_path):
    (tmp_path / "u.txt").write_bytes(b"one\ntwo\n")
    await call(read_file, ctx, file_path="u.txt")
    await call(edit_file, ctx, file_path="u.txt", old_string="two", new_string="2\n3")
    assert (tmp_path / "u.txt").read_bytes() == b"one\n2\n3\n"


async def test_edit_mixed_endings_finds_the_lf_part(ctx, tmp_path):
    (tmp_path / "m.txt").write_bytes(b"a\r\nb\nc\n")
    await call(read_file, ctx, file_path="m.txt")
    out = await call(edit_file, ctx, file_path="m.txt", old_string="b\nc", new_string="B\nC")
    assert not out.is_error
    assert (tmp_path / "m.txt").read_bytes() == b"a\r\nB\nC\n"


async def test_replace_all_counts_with_crlf_pair(ctx, tmp_path):
    (tmp_path / "w.txt").write_bytes(b"x\r\ny\r\nx\r\ny\r\n")
    await call(read_file, ctx, file_path="w.txt")
    out = await call(
        edit_file, ctx, file_path="w.txt", old_string="x\ny", new_string="z\nq", replace_all=True
    )
    assert not out.is_error
    assert (tmp_path / "w.txt").read_bytes() == b"z\r\nq\r\nz\r\nq\r\n"


async def test_overwrite_keeps_crlf_and_new_file_is_lf(ctx, tmp_path):
    (tmp_path / "w.txt").write_bytes(b"x\r\n")
    await call(read_file, ctx, file_path="w.txt")
    await call(write_file, ctx, file_path="w.txt", content="y\nz\n")
    assert (tmp_path / "w.txt").read_bytes() == b"y\r\nz\r\n"
    await call(write_file, ctx, file_path="new.txt", content="a\nb\n")
    assert (tmp_path / "new.txt").read_bytes() == b"a\nb\n"


async def test_cyrillic_round_trip(ctx, tmp_path):
    await call(write_file, ctx, file_path="r.txt", content="привет\n")
    await call(edit_file, ctx, file_path="r.txt", old_string="привет", new_string="мир")
    assert (tmp_path / "r.txt").read_bytes() == "мир\n".encode()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"a\r\nb\r\n", b"a\r\nx\r\ny\r\n"),
        (b"a\r\nb\nc\n", b"a\r\nx\ny\nc\n"),
        (b"a\r\nb", b"a\r\nx\r\ny"),
        (b"a\nb\n", b"a\nx\ny\n"),
    ],
)
async def test_single_line_to_multi_line_follows_match_line(ctx, tmp_path, raw, expected):
    (tmp_path / "s.txt").write_bytes(raw)
    await call(read_file, ctx, file_path="s.txt")
    out = await call(edit_file, ctx, file_path="s.txt", old_string="b", new_string="x\ny")
    assert not out.is_error
    assert (tmp_path / "s.txt").read_bytes() == expected
