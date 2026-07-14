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

# Hardcoded on purpose — these are NOT fetched from Jaga.
#
# The settings page must render whether or not Jaga is reachable: an organization whose Jaga
# is down (or whose service-account password has just expired) still has to be able to open
# its integration settings — if only to turn the sync off. Sourcing the choices over HTTP
# would make this page fail exactly when it is needed most.
#
# Hardcoding costs nothing here, because the values are not per-instance data: they are the
# three categories Jaga groups every one of its statuses under, the same on every deployment.
# The concrete status ids behind them ARE per-space, and those are resolved at sync time
# against the task's own workflow (`issue_config.resolve_target_status`).
CATEGORY_CHOICES = [
    (issue_config.CATEGORY_DONE, "Done"),
    (issue_config.CATEGORY_IN_PROGRESS, "In progress"),
    (issue_config.CATEGORY_TODO, "To do"),
]


class JagaSyncMixin(JagaIssuesMixin, IssueSyncIntegration):
    """Moves (and comments on) the Jaga task whenever the Sentry issue changes status."""

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
        # Every `default` here is mandatory, and each one must match what `sync_status_outbound`
        # falls back to below. Before the first save, `get_config_data()` returns {}: without a
        # default the checkbox would render as off while the sync is in fact on (see
        # `should_sync`), and the very first "Save" would send false and silently kill it.
        return [
            {
                "name": "sync_status_forward",
                "type": "boolean",
                "label": "Sync status to Jaga",
                "help": "Reflect resolving and reopening a Sentry issue in the linked Jaga task.",
                "default": True,
            },
            {
                "name": "resolved_status_category",
                "type": "select",
                "label": "Status to move the task to when the Sentry issue is resolved",
                "help": (
                    "Jaga groups every status under one of these categories. The task is moved "
                    "to the first status of the chosen category its workflow can actually reach "
                    "from where it stands; if there is none, a comment is posted instead."
                ),
                "choices": CATEGORY_CHOICES,
                "default": issue_config.CATEGORY_DONE,
            },
            {
                "name": "unresolved_status_category",
                "type": "select",
                "label": "Status to move the task to when the Sentry issue is reopened",
                "help": "Applied when a resolved issue regresses and the error happens again.",
                "choices": CATEGORY_CHOICES,
                "default": issue_config.CATEGORY_TODO,
            },
            {
                "name": "comment_on_status_change",
                "type": "boolean",
                "label": "Also comment on the task",
                "help": (
                    "Post a comment on the Jaga task in addition to moving it. A comment is "
                    "posted regardless whenever the task cannot be moved."
                ),
                "default": True,
            },
        ]

    def sync_status_outbound(
        self, external_issue: ExternalIssue, is_resolved: bool, project_id: int
    ) -> None:
        config = self.org_integration.config if self.org_integration else {}
        try:
            result = issue_config.apply_status_sync(
                self.get_client(),
                external_issue.key,
                is_resolved=is_resolved,
                # The fallbacks repeat the field defaults above, and must keep doing so: an
                # organization that installed the integration and never opened its settings has
                # an empty `config`, and the sync still has to know where to move a task.
                resolved_category=str(
                    config.get("resolved_status_category") or issue_config.CATEGORY_DONE
                ),
                unresolved_category=str(
                    config.get("unresolved_status_category") or issue_config.CATEGORY_TODO
                ),
                post_comment=bool(config.get("comment_on_status_change", True)),
            )
            logger.info(
                "jaga.sync.status_outbound", extra={"key": external_issue.key, "result": result}
            )
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
