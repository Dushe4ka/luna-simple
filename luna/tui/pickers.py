"""Modal pickers for command results: a choice list and a yes/no confirm."""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static


class ChoiceModal(ModalScreen[str | None]):
    """Pick one option (↑↓ + Enter); Esc dismisses with ``None``."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, title: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self):
        """Render the title, the option list and a key hint."""
        with Vertical():
            yield Static(self._title, classes="picker-title")
            items = []
            for value, label in self._options:
                item = ListItem(Label(label))
                item.data_value = value
                items.append(item)
            yield ListView(*items, id="picker-list")
            yield Static("↑↓ выбрать · Enter подтвердить · Esc отмена", classes="picker-hint")

    def on_mount(self) -> None:
        """Focus the list so arrows work immediately."""
        self.query_one("#picker-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Dismiss with the picked option's value."""
        self.dismiss(getattr(event.item, "data_value", None))

    def action_cancel(self) -> None:
        """Esc: dismiss without a choice."""
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """Yes/No question; Esc means No."""

    BINDINGS = [Binding("escape", "no", "Нет")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self):
        """Render the question and Yes/No buttons."""
        with Vertical():
            yield Static(self._question, classes="picker-title")
            yield Button("Да", id="confirm-yes", variant="warning")
            yield Button("Нет", id="confirm-no")

    def on_mount(self) -> None:
        """Default to No, so a stray Enter never confirms."""
        self.query_one("#confirm-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dismiss with True for Yes, False for No."""
        self.dismiss(event.button.id == "confirm-yes")

    def action_no(self) -> None:
        """Esc means No."""
        self.dismiss(False)
