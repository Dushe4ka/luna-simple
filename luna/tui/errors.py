"""Readable Russian text for ``luna_core.client.ServerError`` messages."""

from __future__ import annotations

import re

#: Human-readable text for the server's machine-readable error codes.
_ERROR_TEXT = {
    "workdir_not_trusted": (
        "Папка не отмечена как доверенная. Перезапустите luna в ней и подтвердите доверие."
    ),
    "workdir_not_absolute": "Путь к папке проекта должен быть абсолютным.",
    "workdir_required": "Не указана папка проекта.",
    "session_busy": "Дождитесь окончания ответа, затем повторите.",
    "client_command": "Эту команду выполняет сам TUI.",
    "workdir_mismatch": "Сессия принадлежит другой папке.",
}

_HTTP = re.compile(r"^HTTP (\d+): (.*)$", re.DOTALL)
_TRANSPORT = {
    "cannot reach the Luna server": "Нет связи с сервером Luna",
    "connection to the Luna server dropped": "Связь с сервером Luna прервалась",
}


def readable(exc: Exception) -> str:
    """Return the text to show the user for a failed server call."""
    text = str(exc)
    match = _HTTP.match(text)
    if match:
        status, detail = match.groups()
        return _ERROR_TEXT.get(detail.strip()) or f"Сервер Luna ответил {status}: {detail}"
    head, sep, tail = text.partition(": ")
    if head in _TRANSPORT:
        return f"{_TRANSPORT[head]}{sep}{tail}"
    return text
