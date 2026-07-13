"""Integration metadata shown in Sentry's integration directory."""

from __future__ import annotations

from sentry.integrations.base import (
    FeatureDescription,
    IntegrationFeatures,
    IntegrationMetadata,
)

REPO_URL = "https://github.com/happykust/sentry-jaga"

DESCRIPTION = """
Integration with Jaga, the issue tracker by Rostelecom.

Create Jaga tasks straight from a Sentry issue, link existing tasks, and have the task
automatically updated when the issue is resolved.
""".strip()

FEATURES = [
    FeatureDescription(
        "Create Jaga tasks from a Sentry issue and link existing tasks.",
        IntegrationFeatures.ISSUE_BASIC,
    ),
    FeatureDescription(
        "Sync the status: when a Sentry issue is resolved, a comment is added to the Jaga task.",
        IntegrationFeatures.ISSUE_SYNC,
    ),
    FeatureDescription(
        "Create a Jaga task automatically from an issue alert rule.",
        IntegrationFeatures.TICKET_RULES,
    ),
]

JAGA_METADATA = IntegrationMetadata(
    description=DESCRIPTION,
    features=FEATURES,
    author="Kirill Nikolaevskiy",
    noun="Installation",
    issue_url=f"{REPO_URL}/issues/new",
    source_url=REPO_URL,
    aspects={},
)
