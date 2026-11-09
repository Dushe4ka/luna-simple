"""Modal pickers for command results: a choice list and a yes/no confirm."""

from __future__ import annotations

from collections.abc import Callable

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static


class ChoiceModal(ModalScreen[str | None]):
    """Pick one option (↑↓ + Enter); Esc dismisses with ``None``."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]
    HINT = "↑↓ выбрать · Enter подтвердить · Esc отмена"

    def __init__(
        self, title: str, options: list[tuple[str, str]], *, initial: str | None = None
    ) -> None:
        super().__init__()
        self._title = title
        self._options = options
        values = [value for value, _ in options]
        #: Row highlighted on open: the option whose value is ``initial`` (else the first).
        self._initial_index = values.index(initial) if initial in values else 0

    def compose(self):
        """Render the title, the option list and a key hint."""
        with Vertical():
            yield Static(self._title, classes="picker-title", markup=False)
            items = []
            for value, label in self._options:
                item = ListItem(Label(label, markup=False))
                item.data_value = value
                items.append(item)
            yield ListView(*items, id="picker-list", initial_index=self._initial_index)
            yield Static(self.HINT, classes="picker-hint")

    def on_mount(self) -> None:
        """Focus the list so arrows work immediately."""
        self.query_one("#picker-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Dismiss with the picked option's value."""
        self.dismiss(getattr(event.item, "data_value", None))

    def action_cancel(self) -> None:
        """Esc: dismiss without a choice."""
        self.dismiss(None)


class ThemeModal(ChoiceModal):
    """Theme picker: the highlighted theme is applied live; Esc restores ``current``."""

    HINT = "↑↓ просмотр · Enter сохранить · Esc — оставить"

    def __init__(
        self,
        options: list[tuple[str, str]],
        *,
        current: str,
        on_preview: Callable[[str], object],
    ) -> None:
        super().__init__("Тема оформления", options, initial=current)
        self._current = current
        self._on_preview = on_preview

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Preview the highlighted theme on the whole app."""
        value = getattr(event.item, "data_value", None)
        if value is not None:
            self._on_preview(value)

    def action_cancel(self) -> None:
        """Esc: put the theme in use back, then dismiss without a choice."""
        self._on_preview(self._current)
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
            yield Static(self._question, classes="picker-title", markup=False)
            with Horizontal(classes="decision-row"):
                yield Button("Да", id="confirm-yes", classes="accent-warn")
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
