import os
from pathlib import Path

import pytest
from luna_core.workspace import OutsideWorkspace, Workspace


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    return Workspace(tmp_path)


def test_relative_and_root_forms(ws):
    assert ws.resolve("src/a.py") == ws.root / "src" / "a.py"
    assert ws.resolve("") == ws.root and ws.resolve(".") == ws.root and ws.resolve("/") == ws.root
    assert ws.rel(ws.root) == "." and ws.rel(ws.root / "src" / "a.py") == "src/a.py"


def test_virtual_root_paths_map_into_the_project(ws):
    assert ws.resolve("/src/a.py") == ws.root / "src" / "a.py"
    assert ws.resolve("/brand-new-dir/x.md") == ws.root / "brand-new-dir" / "x.md"


def test_real_absolute_path_inside_the_root_is_accepted(ws):
    assert ws.resolve(str(ws.root / "src" / "a.py")) == ws.root / "src" / "a.py"


def test_outside_path_is_an_error_not_remapped(ws):
    host_dir = next(p for p in ("/Users", "/home", "/tmp", "/etc") if Path(p).is_dir())
    with pytest.raises(OutsideWorkspace, match="outside the project"):
        ws.resolve(f"{host_dir}/me/Desktop/x.txt")
    assert not (ws.root / host_dir.lstrip("/")).exists()


def test_dotdot_escape_is_an_error(ws):
    with pytest.raises(OutsideWorkspace):
        ws.resolve("src/../../etc/passwd")


def test_symlink_escape_is_an_error(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    os.symlink(outside, ws.root / "link")
    with pytest.raises(OutsideWorkspace):
        ws.resolve("link/secret.txt")


@pytest.mark.parametrize("raw", ["C:\\Users\\me\\x.txt", "c:/tmp/x", "\\\\server\\share\\x"])
def test_windows_style_path_is_an_error_not_remapped(ws, raw):
    before = set(ws.root.iterdir())
    with pytest.raises(OutsideWorkspace, match="outside the project"):
        ws.resolve(raw)
    assert set(ws.root.iterdir()) == before


def test_ignored_dirs(ws):
    assert (
        ws.ignored(".git/config")
        and ws.ignored("node_modules/x/y.js")
        and ws.ignored(".luna/undo/a")
    )
    assert not ws.ignored("src/a.py")
