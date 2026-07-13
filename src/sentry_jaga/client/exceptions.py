"""Jaga client exceptions."""

from __future__ import annotations

from typing import Any


class JagaError(Exception):
    """Base error of the Jaga integration."""


class JagaApiError(JagaError):
    """An HTTP request to the Jaga API failed."""

    def __init__(self, status_code: int, message: str, body: Any = None) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Jaga returned {status_code}: {message}")


class JagaAuthError(JagaApiError):
    """Invalid credentials or an expired token (401/403)."""


class JagaNotFoundError(JagaApiError):
    """The object was not found (404)."""


class JagaRateLimitedError(JagaApiError):
    """The request rate limit was exceeded (429)."""


class JagaServerError(JagaApiError):
    """Jaga failed internally (5xx)."""


def _extract_message(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return "unknown error"


def error_from_response(status_code: int, body: Any) -> JagaApiError:
    """Build a typed exception from the HTTP status of a Jaga response."""
    message = _extract_message(body)
    if status_code in (401, 403):
        return JagaAuthError(status_code, message, body)
    if status_code == 404:
        return JagaNotFoundError(status_code, message, body)
    if status_code == 429:
        return JagaRateLimitedError(status_code, message, body)
    if status_code >= 500:
        return JagaServerError(status_code, message, body)
    return JagaApiError(status_code, message, body)
