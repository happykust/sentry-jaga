"""Провайдер интеграции Sentry ↔ Яга и класс инсталляции."""

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
from sentry_jaga.metadata import JAGA_ICON_URL, JAGA_METADATA
from sentry_jaga.pipeline import InstallationConfigView
from sentry_jaga.sync import JagaSyncMixin


class DjangoTokenCache:
    """Адаптер Django cache под протокол TokenCache ядра."""

    def get(self, key: str) -> dict[str, str] | None:
        value = django_cache.get(key)
        return value if isinstance(value, dict) else None

    def set(self, key: str, value: dict[str, str], timeout: int) -> None:
        django_cache.set(key, value, timeout=timeout)

    def delete(self, key: str) -> None:
        django_cache.delete(key)


class JagaIntegration(JagaSyncMixin, IntegrationInstallation):
    """Инсталляция интеграции для конкретной организации."""

    def get_client(self) -> JagaClient:
        metadata = self.model.metadata
        return JagaClient(
            instance_url=metadata["instance_url"],
            email=metadata["email"],
            password=metadata["password"],
            cache=DjangoTokenCache(),
        )


class JagaIntegrationProvider(IntegrationProvider):
    key = "jaga"
    name = "Яга"
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
            "name": f"Яга ({instance_url})",
            "metadata": {
                "instance_url": instance_url,
                "email": data["email"],
                "password": data["password"],
                "icon": JAGA_ICON_URL,
            },
        }
