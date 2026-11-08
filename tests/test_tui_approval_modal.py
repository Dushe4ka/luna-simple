import pytest
from textual import work

from luna.tui.approval_modal import ApprovalModal


class _HostApp:
    """Minimal harness — ApprovalModal is tested via a real Textual App
    run_test() context, not this stub; see the real test below."""


@pytest.mark.asyncio
async def test_approval_modal_dismisses_with_approve_decision():
    from textual.app import App

    class _TestApp(App):
        @work
        async def on_mount(self) -> None:
            self.result = await self.push_screen_wait(
                ApprovalModal(
                    {"action": "write_file", "args": {"file_path": "/a.py", "content": "x"}}
                )
            )

    app = _TestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#approve-button")
        await pilot.pause()
    assert app.result == {"type": "approve"}


@pytest.mark.asyncio
async def test_approval_modal_dismisses_with_reject_decision():
    from textual.app import App

    class _TestApp(App):
        @work
        async def on_mount(self) -> None:
            self.result = await self.push_screen_wait(
                ApprovalModal({"action": "delete", "args": {"file_path": "/a.py"}})
            )

    app = _TestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#reject-button")
        await pilot.pause()
    assert app.result["type"] == "reject"


@pytest.mark.asyncio
async def test_approval_modal_dismisses_with_always_decision():
    from textual.app import App

    class _TestApp(App):
        @work
        async def on_mount(self) -> None:
            self.result = await self.push_screen_wait(
                ApprovalModal(
                    {"action": "write_file", "args": {"file_path": "/a.py", "content": "x"}}
                )
            )

    app = _TestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#always-button")
        await pilot.pause()
    assert app.result["type"] == "approve"
    assert "always" in app.result
    assert app.result["always"]  # suggest_rule produced a non-empty rule string


async def _run_modal(request, steps):
    from textual.app import App

    class _TestApp(App):
        @work
        async def on_mount(self) -> None:
            self.result = await self.push_screen_wait(ApprovalModal(request))

    app = _TestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await steps(app, pilot)
        await pilot.pause()
    return app


async def test_always_allow_uses_the_suggested_rule_of_the_request():
    request = {
        "call_id": "c1",
        "name": "execute",
        "args": {"command": "ls -la"},
        "suggested_rule": "execute:ls *",
    }

    async def steps(app, pilot):
        await pilot.click("#always-button")

    app = await _run_modal(request, steps)
    assert app.result == {"type": "approve", "always": "execute:ls *"}


async def test_edit_prefills_the_command_and_returns_edited_args():
    request = {"call_id": "c1", "name": "execute", "args": {"command": "ls", "timeout": 5}}

    async def steps(app, pilot):
        await pilot.click("#edit-button")
        await pilot.pause()
        field = app.screen.query_one("#edit-command")
        assert field.value == "ls"
        assert app.focused is field
        await pilot.press(*" -la", "enter")

    app = await _run_modal(request, steps)
    assert app.result == {"type": "edit", "args": {"command": "ls -la", "timeout": 5}}


async def test_edit_is_offered_only_for_execute():
    from textual.app import App

    class _TestApp(App):
        def on_mount(self) -> None:
            self.push_screen(ApprovalModal({"name": "delete", "args": {"file_path": "a"}}))

    app = _TestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.screen.query("#edit-button")


async def test_escape_in_the_edit_field_goes_back_to_the_buttons():
    request = {"call_id": "c1", "name": "execute", "args": {"command": "ls"}}

    async def steps(app, pilot):
        await pilot.click("#edit-button")
        await pilot.pause()
        assert app.screen.query_one(".picker-hint")
        await pilot.press("escape")
        await pilot.pause()
        assert not app.screen.query("#edit-command")
        assert not app.screen.query("#edit-hint")
        assert app.screen.query("#always-hint")  # the rule hint survives Esc
        assert app.screen.query_one("#approve-button").display
        await pilot.click("#approve-button")

    app = await _run_modal(request, steps)
    assert app.result == {"type": "approve"}


async def test_an_empty_edited_command_is_refused_and_edit_mode_stays():
    request = {"call_id": "c1", "name": "execute", "args": {"command": "ls"}}

    async def steps(app, pilot):
        await pilot.click("#edit-button")
        await pilot.pause()
        field = app.screen.query_one("#edit-command")
        field.value = "   "
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#edit-command") is field
        assert "пуст" in str(app.screen.query_one("#edit-hint").render())
        assert "Будет сохранено" in str(app.screen.query_one("#always-hint").render())
        field.value = " pwd "
        await pilot.press("enter")

    app = await _run_modal(request, steps)
    assert app.result == {"type": "edit", "args": {"command": "pwd"}}


@pytest.mark.asyncio
async def test_approval_modal_buttons_are_in_russian_and_not_truncated():
    from textual.app import App
    from textual.widgets import Button

    class _TestApp(App):
        @work
        async def on_mount(self) -> None:
            await self.push_screen_wait(
                ApprovalModal({"action": "execute", "args": {"command": "ls"}})
            )

    app = _TestApp()
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause()
        buttons = {b.id: b for b in app.screen.query(Button)}
        labels = {bid: str(b.label) for bid, b in buttons.items()}
        assert labels == {
            "approve-button": "Разрешить",
            "always-button": "Разрешить всегда",
            "edit-button": "Изменить",
            "reject-button": "Отклонить",
        }
        for button in buttons.values():
            assert button.content_region.width >= len(str(button.label))


@pytest.mark.parametrize(
    ("request_extra", "expected"),
    [
        (
            {
                "suggested_rule": "execute:cd *",
                "suggested_rules": ["execute:cd *", "execute:ls *"],
            },
            "Будет сохранено: execute:cd *, execute:ls *",
        ),
        ({"suggested_rule": "execute:* *"}, "не будет сохранено"),
    ],
)
async def test_always_hint_names_the_rules_that_will_be_saved(request_extra, expected):
    from textual.widgets import Static

    request = {"name": "execute", "args": {"command": "cd src && ls"}, **request_extra}
    seen = {}

    async def steps(app, pilot):
        hint = app.screen.query_one("#always-hint", Static)
        seen["text"] = str(hint.render())
        await pilot.click("#reject-button")

    await _run_modal(request, steps)
    assert expected in seen["text"]


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        ({"name": "write_file", "args": {"file_path": "*"}}, "Будет сохранено: write_file:[*]"),
        (
            {"name": "delete", "args": {"file_path": "x"}, "suggested_rule": "delete:*"},
            "не будет сохранено",
        ),
    ],
)
async def test_always_hint_for_file_tools(request_, expected):
    from textual.widgets import Static

    seen = {}

    async def steps(app, pilot):
        seen["text"] = str(app.screen.query_one("#always-hint", Static).render())
        await pilot.click("#reject-button")

    await _run_modal(request_, steps)
    assert expected in seen["text"]


@pytest.mark.parametrize("command", ['echo "[/]"', "echo ${arr[$i]} [b]x[/b]"])
async def test_approval_modal_shows_a_markup_like_command_literally(command):
    """Regression: the body went through markup, so ``[/]`` raised MarkupError."""
    from textual.app import App
    from textual.widgets import Static

    class _TestApp(App):
        def on_mount(self) -> None:
            self.push_screen(ApprovalModal({"name": "execute", "args": {"command": command}}))

    app = _TestApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        body = app.screen.query(Static).first()
        assert str(body.render()) == f"$ {command}"


async def _run_styled(modal, steps, size=(130, 36)):
    """Like ``_run_modal`` but inside ``LunaApp``, so ``luna.tcss`` applies."""
    from tests.tui_fakes import make_app

    app = make_app(theme_choice="moonlight")
    app.result = None
    async with app.run_test(size=size) as pilot:
        app.push_screen(modal, callback=lambda value: setattr(app, "result", value))
        await pilot.pause()
        await steps(app, pilot)
        await pilot.pause()
    return app


_EXECUTE = {"name": "execute", "args": {"command": "ls"}}
_ORDER = ["approve-button", "always-button", "edit-button", "reject-button"]


async def test_buttons_sit_in_one_compact_row_under_the_hint():
    from textual.widgets import Button

    async def steps(app, pilot):
        buttons = list(app.screen.query(Button))
        assert [b.id for b in buttons] == _ORDER
        assert len({b.region.y for b in buttons}) == 1
        assert all(b.region.height == 1 for b in buttons)
        xs = [b.region.x for b in buttons]
        assert xs == sorted(xs)
        for left, right in zip(buttons, buttons[1:], strict=False):
            assert right.region.x - left.region.right == 1  # one-column gap
        hint = app.screen.query_one("#always-hint")
        assert hint.region.bottom <= buttons[0].region.y
        await pilot.click("#reject-button")

    await _run_styled(ApprovalModal(_EXECUTE), steps)


async def test_modal_width_follows_a_short_command():
    from textual.containers import Vertical

    async def steps(app, pilot):
        box = app.screen.query_one(Vertical)
        assert 50 <= box.region.width < 0.8 * app.size.width - 10
        await pilot.click("#reject-button")

    await _run_styled(ApprovalModal(_EXECUTE), steps)


async def test_tab_cycles_buttons_in_visual_order_and_enter_activates():
    seen = []

    async def steps(app, pilot):
        seen.append(app.focused.id)
        for _ in range(3):
            await pilot.press("tab")
            seen.append(app.focused.id)
        await pilot.press("tab")
        seen.append(app.focused.id)
        await pilot.press("shift+tab")
        seen.append(app.focused.id)
        await pilot.press("enter")

    app = await _run_styled(ApprovalModal(_EXECUTE), steps)
    assert seen == [*_ORDER, "approve-button", "reject-button"]
    assert app.result["type"] == "reject"


async def test_enter_on_the_default_focus_approves():
    async def steps(app, pilot):
        assert app.focused.id == "approve-button"
        await pilot.press("enter")

    app = await _run_styled(ApprovalModal(_EXECUTE), steps)
    assert app.result == {"type": "approve"}


async def test_confirm_modal_uses_the_same_compact_row():
    from textual.widgets import Button

    from luna.tui.pickers import ConfirmModal

    async def steps(app, pilot):
        buttons = list(app.screen.query(Button))
        assert [b.id for b in buttons] == ["confirm-yes", "confirm-no"]
        assert len({b.region.y for b in buttons}) == 1
        assert all(b.region.height == 1 for b in buttons)
        assert app.focused.id == "confirm-no"
        await pilot.press("enter")

    app = await _run_styled(ConfirmModal("точно?"), steps)
    assert app.result is False


async def test_a_long_diff_scrolls_and_the_buttons_stay_on_screen():
    from textual.widgets import Button

    old = "\n".join(f"line_{i} = {i}" for i in range(80))
    request = {
        "name": "edit_file",
        "args": {"file_path": "a.py", "old_string": old, "new_string": old.replace("= 1", "= 2")},
    }

    async def steps(app, pilot):
        for button in app.screen.query(Button):
            assert button.region.bottom <= app.size.height
            assert button.region.height == 1
        assert app.focused.id == "approve-button"  # the scroll box takes no focus
        await pilot.press("enter")

    app = await _run_styled(ApprovalModal(request), steps)
    assert app.result == {"type": "approve"}


async def test_edit_mode_keeps_the_modal_width():
    from textual.containers import Vertical

    widths = []

    async def steps(app, pilot):
        box = app.screen.query_one(Vertical)
        widths.append(box.region.width)
        await pilot.click("#edit-button")
        await pilot.pause()
        widths.append(box.region.width)
        await pilot.press("escape")
        await pilot.pause()
        widths.append(box.region.width)
        await pilot.click("#reject-button")

    await _run_styled(ApprovalModal(_EXECUTE), steps)
    assert widths[0] == widths[1] == widths[2]
