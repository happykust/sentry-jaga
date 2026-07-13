"""One-way status sync, Sentry -> Jaga (a thin delegate)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sentry.integrations.mixins.issues import IssueSyncIntegration, ResolveSyncAction
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.users.services.user import RpcUser

from sentry_jaga import issue_config
from sentry_jaga.client.exceptions import JagaError
from sentry_jaga.issues import JagaIssuesMixin

logger = logging.getLogger("sentry_jaga.sync")


class JagaSyncMixin(JagaIssuesMixin, IssueSyncIntegration):
    """Comments on the Jaga task whenever the Sentry issue changes status."""

    outbound_status_key = "sync_status_forward"
    # Assignee sync and inbound sync (Jaga -> Sentry webhooks) are not supported in this
    # version.
    outbound_assignee_key = None
    inbound_status_key = None
    inbound_assignee_key = None

    def should_sync(self, attribute: str, sync_source: Any = None) -> bool:
        if attribute != "outbound_status":
            return False
        config = self.org_integration.config if self.org_integration else {}
        return bool(config.get("sync_status_forward", True))

    def get_organization_config(self) -> Sequence[Any]:
        # `default` is mandatory: before the first save, `get_config_data()` returns {}, and
        # without it the checkbox would render as off even though the sync is in fact on
        # (see `should_sync`). The very first "Save" would then send false and silently kill
        # the sync.
        return [
            {
                "name": "sync_status_forward",
                "type": "boolean",
                "label": "Sync status to Jaga",
                "help": "Reflect resolving and reopening a Sentry issue in the linked Jaga task.",
                "default": True,
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
            # Jaga being unavailable must not break resolving an issue in Sentry.
            logger.warning(
                "jaga.sync.status_outbound_failed",
                extra={"key": external_issue.key},
                exc_info=True,
            )

    def sync_assignee_outbound(
        self,
        external_issue: ExternalIssue,
        user: RpcUser | None,
        assign: bool = True,
        **kwargs: Any,
    ) -> None:
        # Abstract method of `IssueSyncIntegration`: without it the class stays abstract and
        # Sentry cannot create an installation. Assignee sync is not supported
        # (`outbound_assignee_key = None`, and `should_sync` returns False), so this is a
        # no-op.
        return None

    def get_resolve_sync_action(self, data: Mapping[str, Any]) -> ResolveSyncAction:
        # Inbound Jaga -> Sentry webhooks are not supported in this version.
        return ResolveSyncAction.NOOP
