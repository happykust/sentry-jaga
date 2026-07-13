from datetime import UTC, datetime, timedelta

import pytest

from sentry_jaga.client.auth import InMemoryCache, TokenManager
from sentry_jaga.client.models import Token


def _token(access: str, *, seconds: int = 3600, refresh: str = "rt") -> Token:
    return Token(
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
    )


def _manager(logins: list[Token], refreshes: list[Token]) -> tuple[TokenManager, dict[str, int]]:
    calls = {"login": 0, "refresh": 0}

    def login() -> Token:
        calls["login"] += 1
        return logins.pop(0)

    def refresh(_refresh_token: str) -> Token:
        calls["refresh"] += 1
        return refreshes.pop(0)

    manager = TokenManager(
        login=login,
        refresh=refresh,
        cache=InMemoryCache(),
        cache_key="jaga:test",
    )
    return manager, calls


def test_logs_in_on_first_use() -> None:
    manager, calls = _manager([_token("at1")], [])
    assert manager.get_access_token() == "at1"
    assert calls["login"] == 1


def test_reuses_cached_token() -> None:
    manager, calls = _manager([_token("at1")], [])
    assert manager.get_access_token() == "at1"
    assert manager.get_access_token() == "at1"
    assert calls["login"] == 1


def test_refreshes_when_close_to_expiry() -> None:
    manager, calls = _manager([_token("at1", seconds=5)], [_token("at2")])
    assert manager.get_access_token() == "at2"
    assert calls["login"] == 1
    assert calls["refresh"] == 1


def test_falls_back_to_login_when_refresh_fails() -> None:
    calls = {"login": 0, "refresh": 0}
    tokens = [_token("at1", seconds=5), _token("at3")]

    def login() -> Token:
        calls["login"] += 1
        return tokens.pop(0)

    def refresh(_refresh_token: str) -> Token:
        calls["refresh"] += 1
        raise RuntimeError("refresh rejected")

    manager = TokenManager(
        login=login, refresh=refresh, cache=InMemoryCache(), cache_key="jaga:test"
    )
    assert manager.get_access_token() == "at3"
    assert calls["refresh"] == 1
    assert calls["login"] == 2


def test_invalidate_forces_relogin() -> None:
    manager, calls = _manager([_token("at1"), _token("at2")], [])
    assert manager.get_access_token() == "at1"
    manager.invalidate()
    assert manager.get_access_token() == "at2"
    assert calls["login"] == 2


def test_token_shared_via_cache_between_managers() -> None:
    cache = InMemoryCache()
    calls = {"login": 0}

    def login() -> Token:
        calls["login"] += 1
        return _token("shared")

    def refresh(_rt: str) -> Token:  # pragma: no cover - never called
        raise AssertionError

    first = TokenManager(login=login, refresh=refresh, cache=cache, cache_key="jaga:shared")
    second = TokenManager(login=login, refresh=refresh, cache=cache, cache_key="jaga:shared")

    assert first.get_access_token() == "shared"
    assert second.get_access_token() == "shared"
    assert calls["login"] == 1


@pytest.mark.parametrize(
    "corrupted",
    [
        pytest.param({"garbage": "no token here"}, id="missing-keys"),
        pytest.param(
            {"access_token": "at", "refresh_token": "rt", "expires_at": "not-a-date"},
            id="unparsable-expires-at",
        ),
    ],
)
def test_corrupted_cache_entry_triggers_relogin(corrupted: dict[str, str]) -> None:
    cache = InMemoryCache()
    cache.set("jaga:test", corrupted, timeout=60)

    calls = {"login": 0}

    def login() -> Token:
        calls["login"] += 1
        return _token("fresh")

    def refresh(_rt: str) -> Token:  # pragma: no cover - must never be called
        raise AssertionError("refresh must not be called on a corrupted entry")

    manager = TokenManager(login=login, refresh=refresh, cache=cache, cache_key="jaga:test")

    assert manager.get_access_token() == "fresh"
    assert calls["login"] == 1
