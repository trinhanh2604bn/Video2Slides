"""User-safe Google integration errors (messages are Vietnamese, never contain tokens)."""

from __future__ import annotations


class GoogleError(Exception):
    """A Google API call failed; ``code`` is a stable machine-readable reason."""

    def __init__(self, message: str, code: str = "api_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class GoogleAuthError(GoogleError):
    """Not connected, access revoked, or OAuth misconfigured. The user must (re)connect."""

    def __init__(self, message: str, code: str = "not_connected"):
        super().__init__(message, code)
