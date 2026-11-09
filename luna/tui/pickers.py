"""Modal pickers for command results: a choice list and a yes/no confirm."""

from __future__ import annotations

from collections.abc import Callable

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from luna.commands.base import FILTER_FROM, GROUP_HEADER


class ChoiceModal(ModalScreen[str | None]):
    """Pick one option (↑↓ + Enter); Esc dismisses with ``None``.

    Options whose value is ``GROUP_HEADER`` are group headers: shown, never
    highlighted or picked. A long list gets a filter input on top: typing
    narrows the list (a header stays while its group has a match), ↑↓ still
    move the highlight and Enter picks it.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Отмена"),
        Binding("down", "cursor_down", show=False),
        Binding("up", "cursor_up", show=False),
    ]
    HINT = "↑↓ выбрать · Enter подтвердить · Esc отмена"
    FILTER_HINT = "печатайте для фильтра · ↑↓ выбрать · Enter подтвердить · Esc отмена"

    def __init__(
        self, title: str, options: list[tuple[str, str]], *, initial: str | None = None
    ) -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._initial = initial
        pickable = sum(1 for value, _ in options if value != GROUP_HEADER)
        self._filterable = pickable >= FILTER_FROM

    def _items(self, needle: str = "") -> list[ListItem]:
        """List rows matching ``needle`` (case-insensitive), headers kept for their groups."""
        needle = needle.strip().lower()
        items: list[ListItem] = []
        header: str | None = None
        for value, label in self._options:
            if value == GROUP_HEADER:
                header = label
                continue
            if needle and needle not in label.lower() and needle not in value.lower():
                continue
            if header is not None:
                row = ListItem(Label(header, markup=False), classes="picker-group", disabled=True)
                row.data_value = None
                items.append(row)
                header = None
            item = ListItem(Label(label, markup=False))
            item.data_value = value
            items.append(item)
        return items

    @staticmethod
    def _index_of(items: list[ListItem], value: str | None) -> int | None:
        """Index of the row for ``value``, else of the first pickable row."""
        pickable = [i for i, item in enumerate(items) if not item.disabled]
        for i in pickable:
            if items[i].data_value == value:
                return i
        return pickable[0] if pickable else None

    def compose(self):
        """Render the title, the filter (long lists), the option list and a key hint."""
        items = self._items()
        with Vertical():
            yield Static(self._title, classes="picker-title", markup=False)
            if self._filterable:
                yield Input(placeholder="фильтр…", id="picker-filter")
            yield ListView(
                *items, id="picker-list", initial_index=self._index_of(items, self._initial)
            )
            yield Static("ничего не найдено", id="picker-empty", classes="picker-hint")
            yield Static(self.FILTER_HINT if self._filterable else self.HINT, classes="picker-hint")

    def on_mount(self) -> None:
        """Focus the filter (long lists) or the list, so typing/arrows work at once."""
        self.query_one("#picker-empty").display = False
        self.set_class(self._filterable, "-filterable")
        target = "#picker-filter" if self._filterable else "#picker-list"
        self.query_one(target).focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        """Narrow the list to the rows matching the filter text."""
        event.stop()
        items = self._items(event.value)
        listing = self.query_one("#picker-list", ListView)
        await listing.clear()
        await listing.extend(items)
        listing.index = self._index_of(items, None)
        self.query_one("#picker-empty").display = not items

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter picks the highlighted row (nothing when no row matches)."""
        event.stop()
        item = self.query_one("#picker-list", ListView).highlighted_child
        if item is not None and not item.disabled:
            self.dismiss(getattr(item, "data_value", None))

    def action_cursor_down(self) -> None:
        """↓ while typing in the filter moves the list highlight."""
        self.query_one("#picker-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        """↑ while typing in the filter moves the list highlight."""
        self.query_one("#picker-list", ListView).action_cursor_up()

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
