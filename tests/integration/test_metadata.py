import pytest

pytest.importorskip("sentry")

from sentry.integrations.base import IntegrationFeatures

from sentry_jaga.metadata import FEATURES, JAGA_METADATA


def test_metadata_declares_issue_features() -> None:
    gates = {feature.featureGate for feature in FEATURES}
    assert IntegrationFeatures.ISSUE_BASIC in gates
    assert IntegrationFeatures.ISSUE_SYNC in gates


def test_metadata_has_author_and_urls() -> None:
    assert JAGA_METADATA.author
    assert JAGA_METADATA.source_url.startswith("https://")
    assert JAGA_METADATA.issue_url.startswith("https://")


def test_app_config_points_to_package() -> None:
    from sentry_jaga.apps import JagaAppConfig

    assert JagaAppConfig.name == "sentry_jaga"
