import pytest

from luna.memory import append_note, memory_files


def test_append_creates_dated_section(tmp_path):
    p = append_note(str(tmp_path), "failures", "lib X", "conflicts with 3.13")
    assert p.name == "failures.md"
    body = p.read_text()
    assert "lib X" in body and "conflicts with 3.13" in body and body.lstrip().startswith("## ")


def test_append_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        append_note(str(tmp_path), "random", "t", "n")


def test_memory_files_sorted(tmp_path):
    d = tmp_path / ".luna" / "memory"
    d.mkdir(parents=True)
    (d / "project.md").write_text("x")
    (d / "failures.md").write_text("y")
    assert memory_files(str(tmp_path)) == [".luna/memory/failures.md", ".luna/memory/project.md"]
