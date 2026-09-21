import pytest


def test_detect_language_from_markers(tmp_path):
    from luna.extensions.lspnav import detect_language

    assert detect_language(str(tmp_path)) is None
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect_language(str(tmp_path)) == "python"


def test_detect_language_typescript(tmp_path):
    from luna.extensions.lspnav import detect_language

    (tmp_path / "tsconfig.json").write_text("{}")
    assert detect_language(str(tmp_path)) == "typescript"


def test_available_reflects_import(monkeypatch):
    import luna.extensions.lspnav as lspnav

    assert isinstance(lspnav.available(), bool)


def test_make_tools_without_multilspy_reports_unavailable(tmp_path, monkeypatch):
    import luna.extensions.lspnav as lspnav

    monkeypatch.setattr(lspnav, "available", lambda: False)
    tools = lspnav.make_tools(str(tmp_path), "python")
    assert tools == []


@pytest.mark.skipif(
    not __import__("luna.extensions.lspnav", fromlist=["available"]).available(),
    reason="multilspy not installed",
)
def test_goto_definition_runs_against_a_real_python_file(tmp_path):
    from luna.extensions.lspnav import make_tools

    (tmp_path / "a.py").write_text("def foo():\n    pass\n\nfoo()\n")
    tools = make_tools(str(tmp_path), "python")
    by_name = {t.name: t for t in tools}
    result = by_name["goto_definition"].invoke({"file": "a.py", "line": 4, "symbol": "foo"})
    assert "a.py" in result


def test_symbol_range_without_multilspy_reports_unavailable(tmp_path, monkeypatch):
    import luna.extensions.lspnav as lspnav

    monkeypatch.setattr(lspnav, "available", lambda: False)
    tools = lspnav.make_tools(str(tmp_path), "python")
    assert tools == []


@pytest.mark.skipif(
    not __import__("luna.extensions.lspnav", fromlist=["available"]).available(),
    reason="multilspy not installed",
)
def test_symbol_range_returns_exact_source_text(tmp_path):
    from luna.extensions.lspnav import make_tools

    (tmp_path / "a.py").write_text("def foo():\n    return 1\n\n\ndef bar():\n    return foo()\n")
    tools = make_tools(str(tmp_path), "python")
    by_name = {t.name: t for t in tools}
    result = by_name["symbol_range"].invoke({"file": "a.py", "symbol": "bar"})
    assert "a.py" in result
    assert "def bar():" in result
    assert "return foo()" in result
    assert "def foo():" not in result


@pytest.mark.skipif(
    not __import__("luna.extensions.lspnav", fromlist=["available"]).available(),
    reason="multilspy not installed",
)
def test_symbol_range_reports_no_match(tmp_path):
    from luna.extensions.lspnav import make_tools

    (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
    tools = make_tools(str(tmp_path), "python")
    by_name = {t.name: t for t in tools}
    result = by_name["symbol_range"].invoke({"file": "a.py", "symbol": "does_not_exist"})
    assert "no symbol named" in result


@pytest.mark.skipif(
    not __import__("luna.extensions.lspnav", fromlist=["available"]).available(),
    reason="multilspy not installed",
)
def test_symbol_range_disambiguates_with_line(tmp_path):
    from luna.extensions.lspnav import make_tools

    (tmp_path / "a.py").write_text(
        "class A:\n"
        "    def foo(self):\n"
        "        return 1\n\n\n"
        "class B:\n"
        "    def foo(self):\n"
        "        return 2\n"
    )
    tools = make_tools(str(tmp_path), "python")
    by_name = {t.name: t for t in tools}
    result = by_name["symbol_range"].invoke({"file": "a.py", "symbol": "foo", "line": 7})
    assert "return 2" in result
    assert "return 1" not in result
