"""Harness for the integration tests: they run inside Sentry's own test environment.

Two things Sentry's pytest plugin does not do for us:

1. REGISTERING THE PROVIDER. `IntegrationTestCase` looks the provider up in
   `sentry.integrations.manager.default_manager` by key. `SENTRY_DEFAULT_INTEGRATIONS` — the
   setting an admin would use in production — is read once by `initialize_app()`, before any
   conftest could append to it, so we call `default_manager.register(...)` instead, exactly as
   Sentry registers its own example providers. `trylast=True` is load-bearing: pluggy calls hooks
   last-registered-first, so a plain `pytest_configure` here would run before `django.setup()`,
   when `sentry.integrations.base` is not even importable.

2. THE AUTOUSE FIXTURES OF SENTRY'S ROOT CONFTEST, which `sentry.testutils.cases` relies on. It
   cannot be loaded from here (both repositories have a top-level `tests` package, so
   `-p tests.conftest` shadows ours), so the same fixtures are wired below out of the same public
   helpers. `simulate_on_commit` is strictly required, not a guard: without it every RPC inside a
   test's wrapping transaction — `self.create_organization()` included — trips
   `in_test_assert_no_transaction`.

The module must stay importable with no `sentry` installed: the unit run collects this directory
too (the tests themselves skip via `pytest.importorskip("sentry")`).
"""

from __future__ import annotations

from collections.abc import Generator, MutableMapping

import pytest

try:
    import sentry  # noqa: F401
except ImportError:
    SENTRY_INSTALLED = False
else:
    SENTRY_INSTALLED = True


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    if not SENTRY_INSTALLED:
        return

    from sentry.integrations.manager import default_manager

    from sentry_jaga.integration import JagaIntegrationProvider

    default_manager.register(JagaIntegrationProvider)


# --- mirrors of sentry/tests/conftest.py autouse fixtures (see the module docstring) ---


@pytest.fixture(autouse=True)
def setup_simulate_on_commit(request: pytest.FixtureRequest) -> Generator[None]:
    if not SENTRY_INSTALLED:
        yield
        return

    from sentry.testutils.hybrid_cloud import simulate_on_commit

    with simulate_on_commit(request):
        yield


@pytest.fixture(autouse=True)
def setup_enforce_monotonic_transactions() -> Generator[None]:
    if not SENTRY_INSTALLED:
        yield
        return

    from sentry.testutils.hybrid_cloud import enforce_no_cross_transaction_interactions

    with enforce_no_cross_transaction_interactions():
        yield


@pytest.fixture(autouse=True)
def validate_silo_mode() -> Generator[None]:
    if not SENTRY_INSTALLED:
        yield
        return

    from sentry.silo.base import SiloMode
    from sentry.testutils.pytest.sentry import get_default_silo_mode_for_test_cases

    expected = get_default_silo_mode_for_test_cases()
    message = f"Possible test leak bug! SiloMode was not reset to {expected} between tests."

    if SiloMode.get_current_mode() != expected:
        raise Exception(message)
    yield
    if SiloMode.get_current_mode() != expected:
        raise Exception(message)


@pytest.fixture(autouse=True)
def audit_hybrid_cloud_writes_and_deletes() -> Generator[None]:
    """Sentry's guard against writes to hybrid-cloud foreign keys outside an outbox context."""
    if not SENTRY_INSTALLED:
        yield
        return

    from django.db import connections
    from sentry.testutils.silo import validate_protected_queries

    debug_cursor_state: MutableMapping[str, bool] = {}
    for conn in connections.all():
        debug_cursor_state[conn.alias] = conn.force_debug_cursor
        conn.queries_log.clear()
        conn.force_debug_cursor = True

    try:
        yield
    finally:
        for conn in connections.all():
            conn.force_debug_cursor = debug_cursor_state[conn.alias]
            validate_protected_queries(conn.queries)


@pytest.fixture(autouse=True)
def clear_caches() -> Generator[None]:
    """The Jaga token lives in the Django cache and would otherwise leak between tests."""
    if not SENTRY_INSTALLED:
        yield
        return

    from django.core.cache import cache

    yield
    cache.clear()


@pytest.fixture(autouse=True)
def check_leaked_responses_mocks() -> Generator[None]:
    if not SENTRY_INSTALLED:
        yield
        return

    import responses

    yield
    leaked = responses.registered()
    if leaked:
        responses.reset()
        leaked_s = "".join(f"- {item}\n" for item in leaked)
        raise AssertionError(
            f"`responses` were leaked outside of the test context:\n{leaked_s}"
            f"(make sure to use `@responses.activate` or `with responses.mock:`)"
        )
