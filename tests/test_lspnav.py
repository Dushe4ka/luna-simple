import pytest


def test_detect_language_from_markers(tmp_path):
    from luna.lspnav import detect_language

    assert detect_language(str(tmp_path)) is None
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect_language(str(tmp_path)) == "python"


def test_detect_language_typescript(tmp_path):
    from luna.lspnav import detect_language

    (tmp_path / "tsconfig.json").write_text("{}")
    assert detect_language(str(tmp_path)) == "typescript"


def test_available_reflects_import(monkeypatch):
    import luna.lspnav as lspnav

    assert isinstance(lspnav.available(), bool)


def test_make_tools_without_multilspy_reports_unavailable(tmp_path, monkeypatch):
    import luna.lspnav as lspnav

    monkeypatch.setattr(lspnav, "available", lambda: False)
    tools = lspnav.make_tools(str(tmp_path), "python")
    assert tools == []


@pytest.mark.skipif(
    not __import__("luna.lspnav", fromlist=["available"]).available(),
    reason="multilspy not installed",
)
def test_goto_definition_runs_against_a_real_python_file(tmp_path):
    from luna.lspnav import make_tools

    (tmp_path / "a.py").write_text("def foo():\n    pass\n\nfoo()\n")
    tools = make_tools(str(tmp_path), "python")
    by_name = {t.name: t for t in tools}
    result = by_name["goto_definition"].invoke({"file": "a.py", "line": 4, "symbol": "foo"})
    assert "a.py" in result
