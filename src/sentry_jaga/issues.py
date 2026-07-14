"""Sentry issue layer: a thin delegate on top of `sentry_jaga.issue_config`.

All the logic (building fields, converting the form, searching) lives in the core and is
covered by unit tests that do not need Sentry. What is left here is pulling data out of
Sentry objects and translating errors.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from django.urls import NoReverseMatch, reverse
from sentry.integrations.mixins.issues import IssueBasicIntegration
from sentry.models.group import Group
from sentry.relay.datascrubbing import scrub_data
from sentry.shared_integrations.exceptions import IntegrationError, IntegrationFormError
from sentry.utils import json
from sentry.utils.http import absolute_uri

from sentry_jaga import issue_config
from sentry_jaga.client.api import JagaClient
from sentry_jaga.client.exceptions import JagaError, JagaNotFoundError
from sentry_jaga.descriptions import build_description, build_title

if TYPE_CHECKING:
    from sentry.models.project import Project
    from sentry.services.eventstore.models import GroupEvent

logger = logging.getLogger("sentry_jaga.issues")


@contextmanager
def as_integration_error() -> Iterator[None]:
    """Translate core errors into exceptions that Sentry understands."""
    try:
        yield
    except JagaNotFoundError as exc:
        raise IntegrationFormError({"externalIssue": str(exc)}) from exc
    except JagaError as exc:
        raise IntegrationError(str(exc)) from exc


class JagaIssuesMixin(IssueBasicIntegration):
    """Implementation of Sentry's issue contract on top of the core."""

    def get_client(self) -> JagaClient:  # overridden in JagaIntegration
        raise NotImplementedError

    @property
    def instance_url(self) -> str:
        url: str = self.model.metadata["instance_url"]
        return url.rstrip("/")

    def get_issue_url(self, key: str) -> str:
        # The browser URL of a task card, shown to humans in Sentry's "Linked Issues"
        # panel. Confirmed against a live Jaga instance: /browse/<code>, not /task/<code>.
        return f"{self.instance_url}/browse/{key}"

    def get_issue_display_name(self, external_issue: Any) -> str:
        if external_issue.title:
            return f"{external_issue.key} — {external_issue.title}"
        return str(external_issue.key)

    def make_external_key(self, data: dict[str, Any]) -> str:
        return str(data["key"])

    def get_persisted_default_config_fields(self) -> Sequence[str]:
        """The create-form fields whose last used value is remembered, per Sentry project.

        Sentry stores them for us: `store_issue_last_defaults` is called by the
        create *and* the link endpoint with the submitted form data, and keeps whatever is
        named here in `org_integration.config["project_issue_defaults"][<project id>]`.

        Reading them back, however, is NOT automatic — whatever
        `store_issue_last_defaults`' docstring says about the values being "merged into the
        field configuration objects" when the integration is serialized. Nothing merges them:
        in Sentry 26.3.1 `get_defaults` has exactly one caller in the whole tree, and it is
        Jira Server's own `get_create_issue_config`. So `get_create_issue_config` below calls
        it too, and passes the result into the core.
        """
        return list(issue_config.PERSISTED_FIELDS)

    # `get_persisted_user_default_config_fields` is deliberately NOT overridden. It exists for
    # fields that are personal rather than shared — Jira Server persists `reporter` there, so
    # that a ticket you file is filed as *you* even though your teammate's default is himself.
    # Our create form has no such field: space, task type and the task-type attributes are all
    # properties of the task, and a team wants them to agree, not to differ per member. Storing
    # them per user would also cost a `UserOption` read on every render of the form for nothing.

    # `get_group_title` / `get_group_description` are overridden rather than merely used to
    # pre-fill the form: the alert-rule ticket action calls them directly on the installation
    # (`create_ticket.utils.create_issue`) to fill the task it creates. Sentry's own defaults
    # would do there — but they know nothing of Jaga's 255-character cap on the title, and
    # their description has a different shape from the one a manual create produces. Both
    # paths route through here, so a rule-created task and a hand-created one come out alike.

    def get_group_title(self, group: Group, event: Any, **kwargs: Any) -> str:
        return build_title(group.title)

    def get_group_description(self, group: Group, event: Any, **kwargs: Any) -> str:
        # `IssueBasicIntegration.get_group_body` is not annotated in Sentry, and strict mode
        # forbids calling untyped functions. The ignore is only needed when checking against
        # the Sentry sources (see warn_unused_ignores for this module).
        body: str = (
            self.get_group_body(group, event)  # type: ignore[no-untyped-call]
            if event is not None
            else ""
        )
        return build_description(self._sentry_url(group), group.culprit or "", body)

    def _defaults_from_group(self, group: Group | None) -> tuple[str, str]:
        """Pre-fill the task title and description with Sentry issue data."""
        if group is None:
            return "", ""
        event = group.get_latest_event()
        return self.get_group_title(group, event), self.get_group_description(group, event)

    def get_create_issue_config(
        self, group: Group | None, user: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        title, description = self._defaults_from_group(group)
        # The space and the task type this Sentry project last filed into (see
        # `get_persisted_default_config_fields`). Without a group there is no project to look
        # them up by — the alert-rule ticket action renders the form that way.
        #
        # The ignore is for the run against the Sentry sources, where `get_defaults` is
        # unannotated and strict mode forbids calling it (cf. `get_group_body` above; this
        # module opts out of `warn_unused_ignores` for exactly that reason).
        defaults: dict[str, Any] = (
            self.get_defaults(group.project, user)  # type: ignore[no-untyped-call]
            if group is not None
            else {}
        )
        with as_integration_error():
            return issue_config.build_create_config(
                self.get_client(),
                kwargs.get("params") or {},
                title,
                description,
                defaults=defaults,
                # The one and only way the issue reaches `create_issue`, which Sentry hands the
                # form data and nothing else. Deliberately absent when there is no group — the
                # alert-rule modal renders this form that way and SAVES what it gets, so a group
                # id here would be frozen into the rule and every task it ever files would carry
                # the event of one long-dead issue. See `issue_config.GROUP_ID_FIELD`.
                group_id=str(group.id) if group is not None else None,
            )

    def _org_config(self) -> dict[str, Any]:
        config: dict[str, Any] = self.org_integration.config if self.org_integration else {}
        return config

    def _auto_label(self) -> str:
        """The label to put on the task, as the organization configured it.

        The fallback repeats the field default in `get_organization_config`, and must keep doing
        so: an organization that installed the integration and never opened its settings has an
        empty `config`, and the label is on by default. An admin who cleared the box saves an
        empty string, which is how the feature is turned off — so `.get` must not treat that as
        "unset" and hand back the default again.
        """
        value = self._org_config().get("auto_label", issue_config.DEFAULT_AUTO_LABEL)
        return str(value or "")

    def create_issue(self, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        with as_integration_error():
            result = issue_config.create_task_from_form(
                self.get_client(), data, auto_label=self._auto_label()
            )
        if self._org_config().get("attach_event", False):
            self._attach_event(data, result)
        return result

    def _group_from_form(self, data: dict[str, Any]) -> Group | None:
        """The Sentry issue the create form was opened from, if it said so.

        The id arrives in a hidden form field, which means it arrives from the browser — so it is
        looked up SCOPED TO THIS ORGANIZATION. Unscoped, a hand-edited request would attach the
        latest event of any group on the instance, from any other customer's project, to a Jaga
        task of the attacker's choosing.

        No id at all is the normal case for an alert-rule ticket (`get_create_issue_config` emits
        no field without a group) and simply means: nothing to attach.
        """
        raw = data.get(issue_config.GROUP_ID_FIELD)
        if not raw:
            return None
        # The annotation is for mypy: Django's manager is untyped in this repo's standalone run,
        # so `first()` comes back as Any and strict mode will not return that as `Group | None`.
        group: Group | None = Group.objects.filter(
            id=int(raw), project__organization_id=self.organization_id
        ).first()
        return group

    @staticmethod
    def _event_json(event: GroupEvent, project: Project) -> bytes:
        """The event, scrubbed by Sentry's own scrubber, as the bytes of a JSON file.

        `as_dict()` is the event's "normalized form for external consumers" — what Sentry itself
        serves behind the JSON link on an event page (`EventJsonEndpoint`). The alternative,
        `serialize(event, user, EventSerializer())`, is the shape the Sentry UI renders
        (`entries[]`): it needs a user to serialize for — and on the alert-rule path there is no
        user — and it reshapes the event for a frontend nobody will point at this file.

        `scrub_data` is then the whole of the privacy story, and it is called UNCONDITIONALLY. It
        is the same function, and the same Relay engine, that cleans an event as it comes into
        Sentry: it applies the project's and the organization's `sentry:relay_pii_config` (the PII
        rules an admin wrote by hand), their `sensitive_fields`, `scrub_data`, `scrub_defaults` and
        `scrub_ip_address`. Passing it a project and letting it decide is the only way to be sure
        we honour a setting we have never heard of — hand-written scrubbing of our own would cover
        the fields we thought of on the day, and quietly export the rest. An admin who told Sentry
        to strip `authorization` must not find it in plain text on a Jaga task.

        Running it on the STORED event is also what makes this stricter than Sentry's own JSON
        page, for free. Relay only ever cleaned the events that arrived *after* a setting was
        turned on; the older ones still carry the address, and the page shows it. Here every rule
        is applied to every event at export time, so `user.ip_address` and `request.env.REMOTE_ADDR`
        come out null however old the event is. That page is one authorized person reading one
        event inside Sentry; this file leaves Sentry for a tracker whose audience the admin does
        not control.

        What comes back is Relay's canonical form of the event, so it is not byte-for-byte the
        `as_dict()` we handed in: keys whose value is null are dropped (`dist`, `release`,
        `location` on a typical event), the legacy `message` alias is folded into `logentry`
        (`logentry.formatted` and `title` keep the text), and a `_meta` block records what was
        scrubbed. Nothing of substance is lost.

        A scrub that raises attaches NOTHING: the exception escapes into `_attach_event`, which
        logs it. That is a deliberate departure from `EventJsonEndpoint`, which swallows a failing
        scrub and serves the event anyway — fail-open is a defensible trade for a page, and the
        wrong one for a file we are about to push into another system.
        """
        # `dict(...)`: `scrub_data` hands back the Relay round-trip (`MutableMapping`), and the
        # values in it are plain JSON types — orjson parsed them — so `json.dumps` has nothing
        # exotic left to encode.
        scrubbed = dict(scrub_data(project, event.as_dict()))
        return str(json.dumps(scrubbed)).encode()

    def _attach_event(self, data: dict[str, Any], result: dict[str, Any]) -> None:
        """Attach the JSON of the issue's latest event to the task just created from it.

        Everything here runs AFTER the task exists in Jaga, and that governs the error handling:
        the task is created, it is about to be linked to the Sentry issue, and the user is owed
        that link. An attachment that cannot be made is a warning in the log, never an exception —
        raising would fail the create the user can see, over a file they may not even have noticed
        was coming. The catch is deliberately broad for the same reason: the event comes from
        Snuba/nodestore, and it is not worth enumerating every way a read of those can fail.

        It is also what makes the scrubbing FAIL-CLOSED. `_event_json` scrubs before it serializes;
        if that raises, the exception lands here, the warning is logged and NO file is attached.
        An unscrubbed event is never the fallback.
        """
        key = result.get("key")
        try:
            group = self._group_from_form(data)
            if group is None:
                return

            event = group.get_latest_event()
            if event is None:
                # An issue whose events have aged out of the retention window still has a group.
                logger.info("jaga.issues.no_event_to_attach", extra={"key": key})
                return

            issue_config.attach_event_json(
                self.get_client(),
                space_id=int(data["project"]),
                task_id=int(result["metadata"]["task_id"]),
                event_id=str(event.event_id),
                # `group.project` and not `event.project`: the group is the one thing here that
                # was looked up scoped to this organization (see `_group_from_form`), and the
                # privacy settings must be read off the project the scoping vouched for.
                content=self._event_json(event, group.project),
            )
        except Exception:
            logger.warning("jaga.issues.event_attachment_failed", extra={"key": key}, exc_info=True)

    def _search_url(self, group: Group) -> str | None:
        """The autocomplete endpoint for the link form — if it is reachable at all.

        `sentry_jaga.urls` only exists in the urlconf when the admin has set

            ROOT_URLCONF = "sentry_jaga.urlconf"

        That is optional, and the integration has to work without it. When the route is not
        installed, `reverse` raises `NoReverseMatch` and we hand the core no URL, which puts the
        link form back on its `updatesForm` search. Nothing else changes.
        """
        try:
            # The annotation is for mypy: without Django stubs `reverse()` returns Any, and
            # strict mode will not let that be returned as `str | None`.
            url: str = reverse("sentry-jaga-search", args=[group.organization.slug, self.model.id])
        except NoReverseMatch:
            return None
        return url

    def _sentry_url(self, group: Group) -> str:
        # The annotation is for mypy: `absolute_uri` is untyped without the Sentry sources.
        url: str = absolute_uri(group.get_absolute_url(params={"referrer": "jaga_integration"}))
        return url

    def get_link_issue_config(self, group: Group, **kwargs: Any) -> list[dict[str, Any]]:
        with as_integration_error():
            return issue_config.build_link_config(
                self.get_client(),
                kwargs.get("params") or {},
                search_url=self._search_url(group),
                # The group is in hand here and nowhere else: `after_link_issue` is handed the
                # submitted form and nothing more. So the link travels to it inside the default
                # of the comment field. See `issue_config._comment_field`.
                sentry_url=self._sentry_url(group),
            )

    def after_link_issue(
        self, external_issue: Any, data: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        """Comment on a Jaga task that has just been linked to a Sentry issue.

        The text is whatever stood in the link form's comment box — pre-filled with a link back
        to the Sentry issue, and clearable, which is how a user opts out of a single comment.

        A Jaga failure is swallowed, and that is a deliberate departure from Jira Server, which
        lets it escape. It matters because of *when* this runs: the endpoint has already created
        the `ExternalIssue` but has NOT yet created the `GroupLink`, so an exception here does
        not merely lose the comment — it loses the link the user asked for, and leaves an
        orphaned `ExternalIssue` behind. The link is the point; the comment is a courtesy.
        """
        comment = (data or {}).get("comment")
        if not comment:
            return

        task_id = (getattr(external_issue, "metadata", None) or {}).get("task_id")
        try:
            issue_config.post_task_comment(
                self.get_client(), external_issue.key, str(comment), task_id=task_id
            )
        except JagaError:
            logger.warning(
                "jaga.issues.link_comment_failed",
                extra={"key": external_issue.key},
                exc_info=True,
            )

    def get_issue(self, issue_id: str, **kwargs: Any) -> dict[str, Any]:
        with as_integration_error():
            return issue_config.get_task_summary(self.get_client(), issue_id)

    def search_issues(self, query: str | None, **kwargs: Any) -> list[dict[str, Any]]:
        # No space to scope the search by: linking searches all of Jaga at once (see
        # `issue_config.build_link_config`).
        with as_integration_error():
            return issue_config.search_task_summaries(self.get_client(), query)
