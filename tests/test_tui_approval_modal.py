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
