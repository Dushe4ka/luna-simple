from luna.context import PinnedFiles, expand_mentions, render_pinned


def test_expand_existing_file(tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n")
    out = expand_mentions("look at @a.py please", str(tmp_path))
    assert "<attached: a.py>" in out and "print(1)" in out


def test_expand_quoted_path(tmp_path):
    (tmp_path / "a b.py").write_text("x = 1\n")
    out = expand_mentions('open @"a b.py"', str(tmp_path))
    assert "<attached: a b.py>" in out


def test_expand_missing_file(tmp_path):
    out = expand_mentions("@nope.py", str(tmp_path))
    assert "(@nope.py: not found)" in out


def test_expand_directory_lists(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("")
    out = expand_mentions("@pkg", str(tmp_path))
    assert "m.py" in out


def test_size_cap(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 200_000)
    out = expand_mentions("@big.txt", str(tmp_path))
    assert "truncated" in out and len(out) < 150_000


def test_pinned_roundtrip(tmp_path):
    (tmp_path / "p.py").write_text("P = 1\n")
    pins = PinnedFiles()
    pins.add("p.py")
    assert pins.paths == ["p.py"]
    assert "P = 1" in render_pinned(pins, str(tmp_path))
    pins.drop("p.py")
    assert render_pinned(pins, str(tmp_path)) == ""
