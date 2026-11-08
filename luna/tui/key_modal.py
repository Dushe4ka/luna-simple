"""Masked API-key prompt opened by a command's ``need_key`` effect.

The key is typed into a ``password`` input, sent once through ``save``
(``LunaClient.set_key``) and then wiped from the input: it is never rendered,
logged, put into a toast or kept on the app.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from luna_core.client import ServerError
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from luna.tui.errors import readable


class KeyModal(ModalScreen[bool]):
    """Ask for ``need["provider"]``'s key; dismiss ``True`` once it is stored.

    Enter saves through ``save(key)``; a server error is shown inline and the
    modal stays open. Esc dismisses with ``False``.
    """

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, need: dict, save: Callable[[str], Awaitable[object]]) -> None:
        super().__init__()
        self._need = need
        self._save = save
        self._saving = False

    def compose(self):
        """Render the title, where the key goes, the masked input and an error line."""
        need = self._need
        with Vertical():
            yield Static(f"Ключ для {need['provider']}", classes="picker-title", markup=False)
            yield Static(
                f"{need.get('env_var') or 'API-ключ'} · хранится в "
                f"{need.get('path') or '~/.config/luna/credentials.toml'}, права 0600",
                classes="key-hint",
                markup=False,
            )
            if need.get("hint"):
                yield Static(need["hint"], classes="key-hint", markup=False)
            yield Input(password=True, placeholder="вставьте ключ", id="key-input")
            yield Static("", id="key-error", markup=False)
            yield Static("Enter сохранить · Esc отмена", classes="picker-hint")

    def on_mount(self) -> None:
        """Focus the input so the key can be pasted at once."""
        self.query_one("#key-input", Input).focus()

    def _show_error(self, text: str) -> None:
        error = self.query_one("#key-error", Static)
        error.update(text)
        error.display = bool(text)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter: store the key, or explain why it was not stored."""
        event.stop()
        if self._saving:
            return
        field = event.input
        if not field.value.strip():
            field.value = ""
            self._show_error("Ключ пустой — вставьте ключ или нажмите Esc.")
            return
        self._saving = True
        self._show_error("")
        try:
            await self._save(field.value.strip())
        except ServerError as exc:
            self._show_error(readable(exc))
            return
        except Exception:  # noqa: BLE001 - a crash report (show_locals) could print the key
            self._show_error("Не удалось сохранить ключ. Попробуйте ещё раз или нажмите Esc.")
            return
        finally:
            self._saving = False
        field.value = ""
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Esc: drop whatever was typed and close."""
        self.query_one("#key-input", Input).value = ""
        self.dismiss(False)
