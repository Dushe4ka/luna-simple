"""Approval ModalScreen — replaces the REPL's blocking rich.Panel prompt."""

from __future__ import annotations

from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from luna.core.permissions import suggest_rule
from luna.ui.approve import describe_action


class ApprovalModal(ModalScreen[dict]):
    """Blocks the TUI until the user picks approve/always/reject.

    (``edit`` is not implemented here — see this task's Interfaces note
    for why; the three options here match ``prompt_decision``'s other
    three branches exactly.)

    Styling lives in ``luna/tui/luna.tcss`` (external stylesheet loaded by
    ``LunaApp.CSS_PATH``), not a ``DEFAULT_CSS`` string here — this
    module's own tests mount it under a bare, unstyled ``textual.app.App``
    and only assert on button-click/dismiss behavior, never on layout, so
    that's harmless there.
    """

    def __init__(self, action_request: dict) -> None:
        super().__init__()
        self._action_request = action_request

    def compose(self):
        """Render the pending action's description and the decision buttons."""
        with Vertical():
            yield Static(describe_action(self._action_request))
            yield Button("Approve", id="approve-button", variant="success")
            yield Button("Always allow", id="always-button")
            yield Button("Reject", id="reject-button", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dismiss with the decision dict matching the button pressed."""
        if event.button.id == "approve-button":
            self.dismiss({"type": "approve"})
        elif event.button.id == "always-button":
            action = self._action_request.get("action") or self._action_request.get("name")
            args = self._action_request.get("args", {}) or {}
            rule = suggest_rule(action, args, ".")
            self.dismiss({"type": "approve", "always": rule})
        elif event.button.id == "reject-button":
            self.dismiss({"type": "reject", "message": "rejected in TUI"})
