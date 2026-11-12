"""Документация SDK не обещает установку, которой нет, и описывает Windows."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = [
    *sorted((ROOT / "docs" / "sdk").glob("*.md")),
    ROOT / "packages" / "luna-core" / "README.md",
    ROOT / "README.md",
]
GIT_SPEC = "git+https://github.com/Dushe4ka/luna-simple#subdirectory=packages/luna-core"
PYPI_INSTALL = re.compile(
    r"pip install [\"']?luna-core(\[[a-z,]+\])?(?![\"']?\s*@)[\"']?([\s`)]|$)"
)


def test_no_doc_promises_a_pypi_install_that_does_not_exist():
    bad = [
        f"{p.relative_to(ROOT)}: {line.strip()}"
        for p in DOCS
        for line in p.read_text(encoding="utf-8").splitlines()
        if PYPI_INSTALL.search(line)
    ]
    assert bad == []


def test_pypi_regex_flags_plain_forms_and_accepts_git_form():
    assert PYPI_INSTALL.search("pip install luna-core")
    assert PYPI_INSTALL.search('pip install "luna-core[mcp]"')
    assert PYPI_INSTALL.search('`pip install "luna-core[acp]"` (x)')
    assert not PYPI_INSTALL.search(f'pip install "luna-core @ {GIT_SPEC}"')
    assert not PYPI_INSTALL.search(f'pip install "luna-core[mcp] @ {GIT_SPEC}"')


def test_install_section_uses_the_git_subdirectory():
    text = (ROOT / "docs" / "sdk" / "README.ru.md").read_text(encoding="utf-8")
    assert GIT_SPEC in text


def test_windows_section_covers_the_shell():
    text = (ROOT / "docs" / "sdk" / "README.ru.md").read_text(encoding="utf-8")
    for needle in (
        "Windows",
        "Git Bash",
        "LUNA_GIT_BASH_PATH",
        "luna_core.shell",
        "127",
        "Docker Desktop",
        "%APPDATA%",
    ):
        assert needle in text, needle
