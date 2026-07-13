"""Односторонняя синхронизация статуса Sentry → Яга (тонкий делегат)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sentry.integrations.mixins.issues import IssueSyncIntegration, ResolveSyncAction
from sentry.integrations.models.external_issue import ExternalIssue

from sentry_jaga import issue_config
from sentry_jaga.client.exceptions import JagaError
from sentry_jaga.issues import JagaIssuesMixin

logger = logging.getLogger("sentry_jaga.sync")


class JagaSyncMixin(JagaIssuesMixin, IssueSyncIntegration):
    """Комментирует задачу Яги при смене статуса Sentry-issue."""

    outbound_status_key = "sync_status_forward"
    # Синк ассайни и inbound-синк (вебхуки Яга→Sentry) не поддерживаются в этой версии.
    outbound_assignee_key = None
    inbound_status_key = None
    inbound_assignee_key = None

    def should_sync(self, attribute: str, sync_source: Any = None) -> bool:
        if attribute != "outbound_status":
            return False
        config = self.org_integration.config if self.org_integration else {}
        return bool(config.get("sync_status_forward", True))

    def get_organization_config(self) -> Sequence[Any]:
        return [
            {
                "name": "sync_status_forward",
                "type": "boolean",
                "label": "Синхронизировать статус в Ягу",
                "help": "Отражать закрытие и переоткрытие Sentry-issue в связанной задаче Яги.",
            },
            {
                "name": "comment_on_resolve",
                "type": "boolean",
                "label": "Комментировать задачу",
                "help": "Добавлять комментарий в задачу Яги при смене статуса Sentry-issue.",
            },
        ]

    def sync_status_outbound(
        self, external_issue: ExternalIssue, is_resolved: bool, project_id: int
    ) -> None:
        client = self.get_client()
        try:
            task_id = issue_config.resolve_task_id(
                client, external_issue.key, external_issue.metadata
            )
            client.create_comment(task_id, issue_config.status_comment(is_resolved))
        except JagaError:
            # Недоступность Яги не должна ломать resolve в Sentry.
            logger.warning(
                "jaga.sync.status_outbound_failed",
                extra={"key": external_issue.key},
                exc_info=True,
            )

    def get_resolve_sync_action(self, data: Mapping[str, Any]) -> ResolveSyncAction:
        # Входящие вебхуки Яга→Sentry в этой версии не поддерживаются.
        return ResolveSyncAction.NOOP
