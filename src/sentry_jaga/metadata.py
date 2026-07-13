"""Метаданные интеграции, отображаемые в каталоге интеграций Sentry."""

from __future__ import annotations

from sentry.integrations.base import (
    FeatureDescription,
    IntegrationFeatures,
    IntegrationMetadata,
)

REPO_URL = "https://github.com/happykust/sentry-jaga"

DESCRIPTION = """
Интеграция с Ягой — таск-трекером Ростелекома.

Позволяет создавать задачи в Яге прямо из Sentry-issue, привязывать
существующие задачи и автоматически отмечать в задаче, что issue закрыта.
""".strip()

FEATURES = [
    FeatureDescription(
        "Создавайте задачи в Яге из Sentry-issue и привязывайте существующие задачи.",
        IntegrationFeatures.ISSUE_BASIC,
    ),
    FeatureDescription(
        "Синхронизируйте статус: при закрытии Sentry-issue в задачу Яги добавляется комментарий.",
        IntegrationFeatures.ISSUE_SYNC,
    ),
]

JAGA_METADATA = IntegrationMetadata(
    description=DESCRIPTION,
    features=FEATURES,
    author="Kirill Nikolaevskiy",
    noun="Инсталляция",
    issue_url=f"{REPO_URL}/issues/new",
    source_url=REPO_URL,
    aspects={},
)
