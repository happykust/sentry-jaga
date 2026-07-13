"""Исключения клиента Яги."""

from __future__ import annotations

from typing import Any


class JagaError(Exception):
    """Базовая ошибка интеграции с Ягой."""


class JagaApiError(JagaError):
    """Ошибка HTTP-запроса к API Яги."""

    def __init__(self, status_code: int, message: str, body: Any = None) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Яга вернула {status_code}: {message}")


class JagaAuthError(JagaApiError):
    """Неверные учётные данные или истёкший токен (401/403)."""


class JagaNotFoundError(JagaApiError):
    """Объект не найден (404)."""


class JagaRateLimitedError(JagaApiError):
    """Превышен лимит запросов (429)."""


class JagaServerError(JagaApiError):
    """Внутренняя ошибка Яги (5xx)."""


def _extract_message(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return "неизвестная ошибка"


def error_from_response(status_code: int, body: Any) -> JagaApiError:
    """Собрать типизированное исключение по HTTP-статусу ответа Яги."""
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
