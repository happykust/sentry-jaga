"""Sentry issue layer: a thin delegate on top of `sentry_jaga.issue_config`.

All the logic (building fields, converting the form, searching) lives in the core and is
covered by unit tests that do not need Sentry. What is left here is pulling data out of
Sentry objects and translating errors.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from django.urls import NoReverseMatch, reverse
from sentry.integrations.mixins.issues import IssueBasicIntegration
from sentry.shared_integrations.exceptions import IntegrationError, IntegrationFormError
from sentry.utils.http import absolute_uri

from sentry_jaga import issue_config
from sentry_jaga.client.api import JagaClient
from sentry_jaga.client.exceptions import JagaError, JagaNotFoundError
from sentry_jaga.descriptions import build_description, build_title

if TYPE_CHECKING:
    from sentry.models.group import Group


@contextmanager
def _as_integration_error() -> Iterator[None]:
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
        sentry_url = absolute_uri(group.get_absolute_url(params={"referrer": "jaga_integration"}))
        return build_description(sentry_url, group.culprit or "", body)

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
        with _as_integration_error():
            return issue_config.build_create_config(
                self.get_client(),
                kwargs.get("params") or {},
                title,
                description,
                defaults=defaults,
            )

    def create_issue(self, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        with _as_integration_error():
            return issue_config.create_task_from_form(self.get_client(), data)

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

    def get_link_issue_config(self, group: Group, **kwargs: Any) -> list[dict[str, Any]]:
        with _as_integration_error():
            return issue_config.build_link_config(
                self.get_client(),
                kwargs.get("params") or {},
                search_url=self._search_url(group),
            )

    def get_issue(self, issue_id: str, **kwargs: Any) -> dict[str, Any]:
        with _as_integration_error():
            return issue_config.get_task_summary(self.get_client(), issue_id)

    def search_issues(self, query: str | None, **kwargs: Any) -> list[dict[str, Any]]:
        with _as_integration_error():
            return issue_config.search_task_summaries(
                self.get_client(), kwargs.get("project_id"), query
            )
