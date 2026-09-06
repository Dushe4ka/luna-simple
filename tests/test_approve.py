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
