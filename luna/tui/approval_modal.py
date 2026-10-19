"""Approval ModalScreen — replaces the REPL's blocking rich.Panel prompt."""

from __future__ import annotations

from luna_core.policy import suggest_rule
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from luna.ui.approve import describe_action


class ApprovalModal(ModalScreen[dict]):
    """Blocks the TUI until the user picks approve/always/edit/reject.

    ``Edit`` is offered for ``execute`` only: it swaps the buttons for an
    input prefilled with the command, and submitting it answers
    ``{"type": "edit", "args": {...}}`` with the edited command.

    Styling lives in ``luna/tui/luna.tcss`` (external stylesheet loaded by
    ``LunaApp.CSS_PATH``), not a ``DEFAULT_CSS`` string here.
    """

    def __init__(self, action_request: dict) -> None:
        super().__init__()
        self._action_request = action_request

    @property
    def _tool(self) -> str:
        return self._action_request.get("name") or self._action_request.get("action") or ""

    @property
    def _args(self) -> dict:
        return self._action_request.get("args") or {}

    def compose(self):
        """Render the pending action's description and the decision buttons."""
        with Vertical():
            yield Static(describe_action(self._action_request))
            yield Button("Approve", id="approve-button", variant="success")
            yield Button("Always allow", id="always-button")
            if self._tool == "execute":
                yield Button("Edit", id="edit-button")
            yield Button("Reject", id="reject-button", variant="error")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dismiss with the decision dict matching the button pressed."""
        if event.button.id == "approve-button":
            self.dismiss({"type": "approve"})
        elif event.button.id == "always-button":
            rule = self._action_request.get("suggested_rule") or suggest_rule(
                self._tool, self._args
            )
            self.dismiss({"type": "approve", "always": rule})
        elif event.button.id == "edit-button":
            await self._start_edit()
        elif event.button.id == "reject-button":
            self.dismiss({"type": "reject", "message": "rejected in TUI"})

    async def _start_edit(self) -> None:
        for button in self.query(Button):
            button.display = False
        # no select-all on focus: typing must edit the command, not replace it
        field = Input(
            value=str(self._args.get("command", "")), id="edit-command", select_on_focus=False
        )
        hint = Static("Enter — выполнить изменённую команду · Esc — назад", classes="picker-hint")
        await self.query_one(Vertical).mount(field, hint)
        field.cursor_position = len(field.value)
        field.focus()

    async def on_key(self, event) -> None:
        """Esc in the edit field: drop the edit and show the buttons again."""
        if event.key != "escape" or not self.query("#edit-command"):
            return
        event.stop()
        await self.query_one("#edit-command").remove()
        await self.query(".picker-hint").remove()
        for button in self.query(Button):
            button.display = True
        self.query_one("#approve-button").focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Answer with the edited command (Enter in the edit field)."""
        if event.input.id != "edit-command":
            return
        event.stop()
        command = event.value.strip()
        if not command:
            self.query_one(".picker-hint", Static).update(
                "Команда пуста — введите команду или Esc — назад"
            )
            return
        self.dismiss({"type": "edit", "args": {**self._args, "command": command}})
