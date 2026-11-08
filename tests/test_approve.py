import io

from rich.console import Console

from luna.ui.approve import describe_action, prompt_decision

AR_WRITE = {"action": "write_file", "args": {"file_path": "a.py", "content": "print(1)\n"}}
AR_EXEC = {"action": "execute", "args": {"command": "pytest -q"}}
AR_EDIT = {
    "action": "edit_file",
    "args": {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
}


def _c():
    return Console(file=io.StringIO(), force_terminal=True)


def test_enter_approves():
    d = prompt_decision(_c(), AR_WRITE, input_fn=lambda _: "")
    assert d == {"type": "approve"}


def test_n_rejects_with_reason():
    answers = iter(["n", "wrong file"])
    d = prompt_decision(_c(), AR_WRITE, input_fn=lambda _: next(answers))
    assert d["type"] == "reject"
    assert "wrong file" in d["message"]


def test_edit_execute_replaces_command():
    answers = iter(["e", "pytest -x"])
    d = prompt_decision(_c(), AR_EXEC, input_fn=lambda _: next(answers))
    assert d == {"type": "edit", "args": {"command": "pytest -x"}}


def test_describe_execute_shows_command():
    assert "pytest -q" in describe_action(AR_EXEC)


def test_describe_edit_shows_diff():
    text = describe_action(AR_EDIT)
    assert "-x = 1" in text and "+x = 2" in text


def test_describe_save_skill_is_readable_and_truncated():
    body = "line1\nline2\n" * 50
    ar = {
        "action": "save_skill",
        "args": {"name": "x", "description": "d", "body": body, "project": True},
    }
    text = describe_action(ar)
    assert "save skill 'x' (project scope)" in text
    assert "description: d" in text
    assert "\n" in text  # real newlines, not a single giant repr() line
    assert "more lines" in text  # _truncate kicked in on the 100-line body


def test_always_shows_every_suggested_rule_and_echoes_the_first():
    request = {
        "action": "execute",
        "args": {"command": "cd src && ls"},
        "suggested_rule": "execute:cd *",
        "suggested_rules": ["execute:cd *", "execute:ls *"],
    }
    prompts = []
    answers = iter(["a", ""])

    def ask(prompt):
        prompts.append(prompt)
        return next(answers)

    d = prompt_decision(_c(), request, input_fn=ask)
    assert d == {"type": "approve", "always": "execute:cd *"}
    assert "execute:cd *" in prompts[-1] and "execute:ls *" in prompts[-1]


def test_request_dict_carries_the_suggested_rules():
    from luna_core.events import ApprovalRequest

    from luna.core.session import _request_dict

    r = ApprovalRequest("c", "execute", {"command": "a && b"}, "", "execute:a *", ("x", "y"))
    d = _request_dict(r)
    assert d["suggested_rule"] == "execute:a *" and d["suggested_rules"] == ["x", "y"]


def _spans(text):
    return {str(s.style) for s in text.spans}


def test_edit_diff_uses_theme_styles():
    from rich.text import Text

    from luna.ui.approve import _panel

    body = _panel(AR_EDIT).renderable
    assert isinstance(body, Text)
    assert "luna.diff.add" in _spans(body)
    assert "luna.diff.del" in _spans(body)


def test_panel_title_and_left_bar():
    from luna.ui.approve import _panel

    panel = _panel(AR_WRITE)
    assert "luna.tool.name" in str(panel.title)
    assert panel.border_style is not None
    c = Console(file=io.StringIO(), width=40, record=True)
    c.print(_panel({"action": "write_file", "args": {"file_path": "a.py", "content": "a\nb\nc"}}))
    lines = c.export_text().splitlines()
    assert not any(ch in "".join(lines) for ch in "╭╮╰╯─│")
    body = [ln for ln in lines[1:-1]]
    assert body and all(ln.startswith("┃") for ln in body)


def test_execute_syntax_follows_theme_brightness():
    from rich.syntax import Syntax

    from luna.ui.approve import _panel
    from luna.ui.themes import active, load_theme, set_active

    before = active()
    try:
        set_active(load_theme("daymoon"))
        body = _panel(AR_EXEC).renderable
        assert isinstance(body, Syntax)
        assert body._theme.style_map == Syntax.get_theme("ansi_light").style_map
        set_active(load_theme("moonlight"))
        dark = _panel(AR_EXEC).renderable._theme
        assert dark.style_map == Syntax.get_theme("ansi_dark").style_map
    finally:
        set_active(before)
