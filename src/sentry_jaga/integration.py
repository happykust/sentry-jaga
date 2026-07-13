"""The Sentry <-> Jaga integration provider and its installation class."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from django.core.cache import cache as django_cache
from sentry.integrations.base import (
    IntegrationData,
    IntegrationFeatures,
    IntegrationInstallation,
    IntegrationProvider,
)
from sentry.integrations.pipeline import IntegrationPipeline
from sentry.pipeline.views.base import PipelineView

from sentry_jaga.client.api import JagaClient
from sentry_jaga.metadata import JAGA_METADATA
from sentry_jaga.pipeline import InstallationConfigView
from sentry_jaga.sync import JagaSyncMixin


class DjangoCache:
    """Adapter of the Django cache to the core's `Cache` protocol (token + list of spaces)."""

    def get(self, key: str) -> dict[str, Any] | None:
        value = django_cache.get(key)
        return value if isinstance(value, dict) else None

    def set(self, key: str, value: dict[str, Any], timeout: int) -> None:
        django_cache.set(key, value, timeout=timeout)

    def delete(self, key: str) -> None:
        django_cache.delete(key)


class JagaIntegration(JagaSyncMixin, IntegrationInstallation):
    """The integration installation for a specific organization."""

    def get_client(self) -> JagaClient:
        metadata = self.model.metadata
        return JagaClient(
            instance_url=metadata["instance_url"],
            email=metadata["email"],
            password=metadata["password"],
            cache=DjangoCache(),
        )


def _assert_concrete(cls: type[IntegrationInstallation]) -> None:
    """Concreteness gate for `JagaIntegration` — the only guard the Sentry layer has.

    The Sentry layer is not covered by tests (the `sentry` package is absent from the unit
    run); mypy against the Sentry sources is what holds it. But
    `integration_cls: type[IntegrationInstallation] | None` in `IntegrationProvider` is not
    a `type[T]` parameter, and mypy does NOT check `type-abstract` on assignment: a class
    that lost the implementation of an abstract Sentry method used to sail through the gate
    silently (the regression fixed by a16d2db) and only blew up at runtime.

    Passing the class into a `type[IntegrationInstallation]` parameter turns that check on:
    a forgotten abstract method becomes a `[type-abstract]` error in the "Types against the
    Sentry API" CI job.
    """


_assert_concrete(JagaIntegration)


class JagaIntegrationProvider(IntegrationProvider):
    key = "jaga"
    name = "Jaga"
    metadata = JAGA_METADATA
    integration_cls = JagaIntegration
    features = frozenset([IntegrationFeatures.ISSUE_BASIC, IntegrationFeatures.ISSUE_SYNC])

    def get_pipeline_views(self) -> Sequence[PipelineView[IntegrationPipeline]]:
        return [InstallationConfigView()]

    def build_integration(self, state: Mapping[str, Any]) -> IntegrationData:
        data = state["installation_data"]
        instance_url = data["instance_url"].rstrip("/")
        return {
            "external_id": instance_url,
            "name": f"Jaga ({instance_url})",
            # `icon` is deliberately omitted: there is no logo file in the repository yet,
            # and a broken link to GitHub from an air-gapped network is worse than Sentry's
            # generic icon.
            "metadata": {
                "instance_url": instance_url,
                "email": data["email"],
                "password": data["password"],
            },
        }
