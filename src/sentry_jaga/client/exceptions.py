"""Jaga client exceptions."""

from __future__ import annotations

import json
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


MESSAGE_KEYS = ("message", "error", "detail")
# `_extract_message` unwraps JSON nested inside JSON; a body we do not control must not spin it.
MAX_UNWRAP_DEPTH = 3


def _message_from(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in MESSAGE_KEYS:
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _as_json(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return None


def _extract_message(body: Any) -> str:
    """The human-readable message of a Jaga error.

    Jaga hides it one level down: the body's `error` key holds a JSON *string*, which in turn
    carries the `message`. So a message that itself parses as JSON is unwrapped again; one that
    does not parse is already the message.
    """
    message = _message_from(body)
    if message is None:
        return "unknown error"
    for _ in range(MAX_UNWRAP_DEPTH):
        nested = _message_from(_as_json(message))
        if nested is None:
            break
        message = nested
    return message


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
