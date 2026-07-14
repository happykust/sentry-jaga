"""One-way status sync, Sentry -> Jaga (a thin delegate)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from sentry.integrations.mixins.issues import (
    IntegrationSyncTargetNotFound,
    IssueSyncIntegration,
    ResolveSyncAction,
)
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.users.services.user import RpcUser
from sentry.users.services.user.service import user_service

from sentry_jaga import issue_config
from sentry_jaga.client.exceptions import JagaError
from sentry_jaga.descriptions import UNKNOWN_AUTHOR, build_note_comment
from sentry_jaga.issues import JagaIssuesMixin, as_integration_error

logger = logging.getLogger("sentry_jaga.sync")

# Hardcoded on purpose, NOT fetched from Jaga: the settings page must render even when Jaga is
# down or the service-account password has expired — if only so the sync can be turned off. The
# three categories are the same on every deployment; the concrete status ids behind them are
# per-space and are resolved at sync time (`issue_config.resolve_target_status`).
CATEGORY_CHOICES = [
    (issue_config.CATEGORY_DONE, "Done"),
    (issue_config.CATEGORY_IN_PROGRESS, "In progress"),
    (issue_config.CATEGORY_TODO, "To do"),
]


class JagaSyncMixin(JagaIssuesMixin, IssueSyncIntegration):
    """Moves (and comments on) the Jaga task whenever the Sentry issue changes."""

    outbound_status_key = "sync_status_forward"
    # Sentry's own name for this one: `should_comment_sync` gates the `create_comment` /
    # `update_comment` background tasks on `should_sync("comment")`, which reads this key.
    comment_key = "sync_comments"
    outbound_assignee_key = "sync_assignee_forward"
    # Inbound sync (Jaga -> Sentry webhooks) is not supported in this version.
    inbound_status_key = None
    inbound_assignee_key = None

    # What each sync does before the organization has ever opened its settings, i.e. while
    # `org_integration.config` is still {} (the base `should_sync` hardcodes False there). Every
    # default here must agree with the one rendered by `get_organization_config`: a checkbox that
    # reads "off" while the sync runs is a lie, and the first Save would turn it off for real.
    #
    # Status sync is ON — moving a task is the point of installing this. Comment and assignee sync
    # are OFF, like every issue integration upstream: a Sentry note is internal discussion, and an
    # assignment names a real person in another system and notifies them. Both are decisions for an
    # admin to take on purpose.
    SYNC_DEFAULTS: ClassVar[dict[str, bool]] = {
        "outbound_status": True,
        "comment": False,
        "outbound_assignee": False,
    }

    def should_sync(self, attribute: str, sync_source: Any = None) -> bool:
        if attribute not in self.SYNC_DEFAULTS:
            return False
        key: str = getattr(self, f"{attribute}_key")
        config = self.org_integration.config if self.org_integration else {}
        return bool(config.get(key, self.SYNC_DEFAULTS[attribute]))

    def get_organization_config(self) -> Sequence[Any]:
        # Every `default` here is mandatory and must match what `sync_status_outbound` falls back
        # to below: before the first save `get_config_data()` returns {}. See `SYNC_DEFAULTS`.
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
            {
                "name": "attach_event",
                "type": "boolean",
                "label": "Attach the Sentry event to the task",
                "help": (
                    "Attach the full JSON of the issue's latest event to the task, as a file. "
                    "OFF BY DEFAULT: an event routinely carries personal data — the user's "
                    "email and IP address, the request headers and body — and the Jaga task may "
                    "have a wider audience than the Sentry issue. Turn it on only if that is "
                    "acceptable. Does not apply to tasks filed by an alert rule."
                ),
                "default": False,
            },
            {
                "name": "auto_label",
                "type": "string",
                "label": "Label to put on tasks created from Sentry",
                "help": (
                    "Every task this integration files gets this label, so all of them can be "
                    "found in Jaga with a single filter. The label is created on first use. "
                    "Leave the box empty to add no label. A task type that has no label "
                    "attribute is filed without one."
                ),
                "default": issue_config.DEFAULT_AUTO_LABEL,
            },
            {
                "name": self.outbound_assignee_key,
                "type": "boolean",
                "label": "Sync assignment to Jaga",
                "help": (
                    "Put the Sentry issue's assignee on the linked Jaga task, and take them off "
                    "it again when the issue is unassigned. The two are matched by email address; "
                    "a Sentry user with no Jaga account is skipped, and the task keeps whoever it "
                    "had. Assigning an issue to a Sentry team changes nothing in Jaga. Off by "
                    "default: this names a real person in another system and notifies them."
                ),
                "default": self.SYNC_DEFAULTS["outbound_assignee"],
            },
            {
                "name": self.comment_key,
                "type": "boolean",
                "label": "Sync Sentry comments to Jaga",
                "help": (
                    "Post notes written on a Sentry issue as comments on the linked Jaga task, "
                    "attributed to their author. Off by default: notes are internal discussion, "
                    "and the Jaga task may have a wider audience."
                ),
                "default": self.SYNC_DEFAULTS["comment"],
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
                # organization that never opened its settings has an empty `config`.
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

    # --- Sentry notes -> Jaga comments ---------------------------------------------------
    #
    # Driven by Sentry's `create_comment` / `update_comment` background tasks, gated on
    # `should_sync("comment")` above. `issue_id` is NOT a database id: the tasks pass
    # `external_issue.key`, i.e. the Jaga task code (`issue_config._task_id_for` turns it into the
    # numeric id the comment API wants).
    #
    # Failures are deliberately NOT swallowed here (unlike `sync_status_outbound`): these run in a
    # background task Sentry retries five times, and swallowing would throw the retry away.

    def _author_name(self, user_id: int) -> str:
        """The display name of the Sentry user who wrote the note.

        A note can outlive its author's account, so a missing user is a normal outcome, not an
        error — Jira Server asserts here and would 500.
        """
        user = user_service.get_user(user_id=user_id)
        if user is None:
            return UNKNOWN_AUTHOR
        return str(user.name or user.email or UNKNOWN_AUTHOR)

    def create_comment(self, issue_id: str, user_id: int, group_note: Any) -> dict[str, Any]:
        """Post a Sentry note on the linked Jaga task, and return the comment Jaga created.

        Returning it is load-bearing: `sentry.integrations.tasks.create_comment` feeds the return
        value to `get_comment_id()` and stores it in `note.data["external_id"]`. Return None and a
        later edit of the note has no comment to point at.
        """
        content = build_note_comment(self._author_name(user_id), group_note.data["text"])
        with as_integration_error():
            return issue_config.post_task_comment(self.get_client(), issue_id, content)

    def update_comment(self, issue_id: str, user_id: int, group_note: Any) -> dict[str, Any]:
        """Rewrite the Jaga comment that a previously synced Sentry note created."""
        content = build_note_comment(self._author_name(user_id), group_note.data["text"])
        with as_integration_error():
            return issue_config.edit_task_comment(
                self.get_client(),
                issue_id,
                int(group_note.data["external_id"]),
                content,
            )

    @staticmethod
    def _addresses_of(user: RpcUser) -> list[str]:
        """Every address this Sentry user might be known by in Jaga, best first.

        `RpcUser.emails` holds only the *VERIFIED* addresses, and `UserEmail.is_verified` defaults
        to False — so on a self-hosted Sentry with no working SMTP it is empty for EVERY user.
        Hence the primary address (`user.email`) leads and the verified ones follow, and hence "no
        addresses" must NEVER be read as "unassign" (see `sync_assignee_outbound`).

        `emails` is a frozenset, and iterating one is ordered by a string hash Python randomises
        per process. Sorting keeps the address chosen repeatable across workers.
        """
        addresses = [user.email] if user.email else []
        addresses += sorted(user.emails)
        return list(dict.fromkeys(a for a in addresses if a))

    def sync_assignee_outbound(
        self,
        external_issue: ExternalIssue,
        user: RpcUser | None,
        assign: bool = True,
        **kwargs: Any,
    ) -> None:
        """Put the Sentry issue's assignee on the Jaga task, or take them off it.

        `user is None` means unassign — Sentry's own comment for it is "Assume unassign if None"
        (`sentry/integrations/tasks/sync_assignee_outbound.py`). An issue assigned to a *team* never
        reaches here at all: the outbound sync is only queued when `assignee_type == "user"`
        (`models/groupassignee.py`). Matching is by email; see `_addresses_of`.

        A Sentry user with no Jaga counterpart raises `IntegrationSyncTargetNotFound`, which
        Sentry's task records as a halt. It must NOT fall through to the unassign branch: that would
        take a real person off a real task because a lookup missed.

        Jaga being down propagates as an `IntegrationError` — NOT swallowed, unlike
        `sync_status_outbound`: the task retries five times, and swallowing would throw the retry
        away and record an assignment that never happened.
        """
        if user is not None and assign:
            addresses = self._addresses_of(user)
            with as_integration_error():
                result = issue_config.apply_assignee_sync(
                    self.get_client(), external_issue.key, addresses, assign=True
                )
            if result == issue_config.ASSIGNEE_NOT_FOUND:
                logger.info(
                    "jaga.sync.assignee_not_in_jaga",
                    extra={"key": external_issue.key, "address_count": len(addresses)},
                )
                raise IntegrationSyncTargetNotFound("No Jaga user matches the Sentry assignee.")
        else:
            with as_integration_error():
                result = issue_config.apply_assignee_sync(
                    self.get_client(), external_issue.key, [], assign=False
                )

        logger.info(
            "jaga.sync.assignee_outbound", extra={"key": external_issue.key, "result": result}
        )

    def get_resolve_sync_action(self, data: Mapping[str, Any]) -> ResolveSyncAction:
        # Inbound Jaga -> Sentry webhooks are not supported in this version.
        return ResolveSyncAction.NOOP
