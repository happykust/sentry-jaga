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
    """Moves (and comments on) the Jaga task whenever the Sentry issue changes."""

    outbound_status_key = "sync_status_forward"
    # Sentry's own name for this one: `should_comment_sync` (sentry/integrations/tasks) gates the
    # `create_comment` / `update_comment` background tasks on `should_sync("comment")`, which
    # reads the config under the key named here.
    comment_key = "sync_comments"
    outbound_assignee_key = "sync_assignee_forward"
    # Inbound sync (Jaga -> Sentry webhooks) is not supported in this version.
    inbound_status_key = None
    inbound_assignee_key = None

    # What each sync does before the organization has ever opened its settings — the moment
    # `org_integration.config` is still {}. The base `should_sync` hardcodes False there; we
    # override it because the status sync has to be on out of the box, and because every default
    # below must agree with the one rendered by `get_organization_config` (a checkbox that reads
    # "off" while the sync is in fact running is a lie, and the first Save would then quietly
    # turn it off for real).
    #
    # Status sync defaults ON: moving a task is the whole point of installing this, and it says
    # nothing an incident responder would not want said.
    #
    # Comment sync defaults OFF, like every issue integration upstream. A Sentry note is internal
    # discussion — it can name a customer, a credential, a suspect commit — and forwarding it to
    # a tracker with a different audience is a decision for an admin to take on purpose, not a
    # surprise to discover afterwards.
    #
    # Assignee sync defaults OFF for a plainer reason: it puts a named human on a ticket in
    # another system, which notifies them and makes them accountable for it. Sentry's idea of who
    # owns an issue is not automatically the right answer inside Jaga, where the space may have a
    # duty roster of its own. Whoever wants the two to agree can say so.
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

    # --- Sentry notes -> Jaga comments ---------------------------------------------------
    #
    # Driven by Sentry's `create_comment` / `update_comment` background tasks
    # (sentry/integrations/tasks/), which the group-notes endpoint queues for every external
    # issue linked to the group. Both are gated on `should_sync("comment")` above.
    #
    # `issue_id` is NOT a database id: the tasks pass `external_issue.key`, i.e. the Jaga task
    # code. `_task_id_for` in the core turns it into the numeric id the comment API wants.
    #
    # Failures are deliberately NOT swallowed here (unlike `sync_status_outbound`): these run in
    # a background task that Sentry retries five times. Swallowing would throw away the retry and
    # lose the note for good.

    def _author_name(self, user_id: int) -> str:
        """The display name of the Sentry user who wrote the note.

        This is the one thing the Sentry layer contributes to the comment: the text itself is
        built in the core (`build_note_comment`). A note can outlive its author's account, so a
        missing user is a normal outcome, not an error — Jira Server asserts here and would 500.
        """
        user = user_service.get_user(user_id=user_id)
        if user is None:
            return UNKNOWN_AUTHOR
        return str(user.name or user.email or UNKNOWN_AUTHOR)

    def create_comment(self, issue_id: str, user_id: int, group_note: Any) -> dict[str, Any]:
        """Post a Sentry note on the linked Jaga task, and return the comment Jaga created.

        Returning it is load-bearing: `sentry.integrations.tasks.create_comment` feeds the return
        value to `get_comment_id()` and stores the result in `note.data["external_id"]`. Return
        None and an edit of the note later has no comment to point at.
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

        `RpcUser.emails` is NOT "the user's email addresses" — it is the *verified* ones only
        (`sentry/users/services/user/serial.py`: `[e for e in useremails if e["is_verified"]]`),
        and `UserEmail.is_verified` defaults to False. On a self-hosted Sentry with no working
        SMTP, nobody ever confirms an address, so that set is empty for EVERY user. Relying on it
        alone would mean the assignee sync never matched anyone on exactly the deployments this
        integration is built for. So the primary address (`user.email`) leads, and the verified
        ones follow.

        `emails` is a frozenset, and iterating one is ordered by string hash — which Python
        randomises per process. Left unsorted, a user with two addresses that resolve to two
        different Jaga profiles would land on a different executor depending on which worker ran
        the task. Sorting makes the choice boring and repeatable.
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

        `user is None` means unassign — that is Sentry's own reading of it, in as many words:
        `sentry/integrations/tasks/sync_assignee_outbound.py` resolves the user with
        `user_service.get_user(user_id) if user_id else None` under the comment "Assume unassign
        if None". (Assigning a Sentry issue to a *team* does not reach here at all: the outbound
        sync is only queued when `assignee_type == "user"` — `models/groupassignee.py`.)

        Matching is by email; see `_addresses_of` for which addresses, and in what order.

        A user Jaga has never heard of raises `IntegrationSyncTargetNotFound`, which Sentry's task
        catches and records as a halt. It must NOT fall through to the unassign branch: that would
        take a real person off a real task because Sentry could not find their counterpart — the
        loudest possible way to lose data over a lookup miss.

        Jaga being down propagates as an `IntegrationError`. Unlike `sync_status_outbound`, this
        is NOT swallowed: the task retries five times, and swallowing would throw the retry away
        AND record the assignment as a success that never happened.
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
