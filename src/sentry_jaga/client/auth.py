"""Jaga access token management: login, refresh, caching."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from sentry_jaga.client.models import Token

DEFAULT_LEEWAY_SECONDS = 30


class Cache(Protocol):
    """Minimal cache contract, injected from outside (the core knows nothing about Django).

    A value is a `dict[str, Any]`, not just a token's string fields, because the same cache also
    holds short-lived reference data such as the list of spaces.
    """

    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, value: dict[str, Any], timeout: int) -> None: ...

    def delete(self, key: str) -> None: ...


class InMemoryCache:
    """In-process cache. The default, and convenient in tests."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._data.get(key)

    def set(self, key: str, value: dict[str, Any], timeout: int) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class TokenManager:
    """Hands out a valid access token, renewing it as needed."""

    def __init__(
        self,
        *,
        login: Callable[[], Token],
        refresh: Callable[[str], Token],
        cache: Cache,
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
        # About to expire: try a refresh, fall back to a full re-login on any failure.
        try:
            refreshed = self._refresh(token.refresh_token)
        except Exception:
            refreshed = self._login()
        return self._store(refreshed).access_token

    def invalidate(self) -> None:
        """Drop the cache entry — the next call will perform a full login."""
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
