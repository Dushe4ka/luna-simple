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
        assert "пуст" in str(app.screen.query_one(".picker-hint").render())
        field.value = " pwd "
        await pilot.press("enter")

    app = await _run_modal(request, steps)
    assert app.result == {"type": "edit", "args": {"command": "pwd"}}
