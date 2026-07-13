"""Управление токеном доступа к Яге: логин, рефреш, кэширование."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sentry_jaga.client.models import Token

DEFAULT_LEEWAY_SECONDS = 30


class TokenCache(Protocol):
    """Минимальный контракт кэша токена (совместим с Django cache)."""

    def get(self, key: str) -> dict[str, str] | None: ...

    def set(self, key: str, value: dict[str, str], timeout: int) -> None: ...

    def delete(self, key: str) -> None: ...


class InMemoryTokenCache:
    """Кэш в памяти процесса. Дефолт и удобен в тестах."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, str]] = {}

    def get(self, key: str) -> dict[str, str] | None:
        return self._data.get(key)

    def set(self, key: str, value: dict[str, str], timeout: int) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class TokenManager:
    """Отдаёт валидный access-токен, обновляя его по мере необходимости."""

    def __init__(
        self,
        *,
        login: Callable[[], Token],
        refresh: Callable[[str], Token],
        cache: TokenCache,
        cache_key: str,
        leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
    ) -> None:
        self._login = login
        self._refresh = refresh
        self._cache = cache
        self._cache_key = cache_key
        self._leeway = leeway_seconds

    def get_access_token(self) -> str:
        token = self._read_cache()
        if token is None:
            token = self._store(self._login())
        if not token.is_expired(self._leeway):
            return token.access_token
        # Токен на грани истечения: пробуем рефреш, при любом сбое — полный релогин.
        try:
            refreshed = self._refresh(token.refresh_token)
        except Exception:
            refreshed = self._login()
        return self._store(refreshed).access_token

    def invalidate(self) -> None:
        """Сбросить кэш — следующий вызов сделает полный логин."""
        self._cache.delete(self._cache_key)

    def _read_cache(self) -> Token | None:
        raw = self._cache.get(self._cache_key)
        if raw is None:
            return None
        try:
            return Token.from_dict(raw)
        except (KeyError, ValueError):
            return None

    def _store(self, token: Token) -> Token:
        ttl = int((token.expires_at - datetime.now(UTC)).total_seconds())
        self._cache.set(self._cache_key, token.to_dict(), timeout=max(ttl, 1))
        return token
