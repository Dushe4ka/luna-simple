"""Exceptions raised by the core."""

from __future__ import annotations

KINDS = ("auth", "rate_limit", "context_length", "bad_request", "server", "network", "unknown")
_RETRYABLE = {"rate_limit", "server", "network"}


class LunaCoreError(Exception):
    """Base class for core errors."""


class ProviderError(LunaCoreError):
    """A model provider request failed.

    ``kind`` classifies the failure so callers can decide what to do:
    ``auth`` (bad or missing key), ``rate_limit``, ``context_length``,
    ``bad_request``, ``server``, ``network``, ``unknown``.
    """

    def __init__(
        self,
        message: str,
        kind: str = "unknown",
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown provider error kind {kind!r}")
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        """True for transient failures worth retrying."""
        return self.kind in _RETRYABLE


class ToolError(LunaCoreError):
    """A tool failed in a way the model should see as an error result."""
