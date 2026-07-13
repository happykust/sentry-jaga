"""Sentry issue layer: a thin delegate on top of `sentry_jaga.issue_config`.

All the logic (building fields, converting the form, searching) lives in the core and is
covered by unit tests that do not need Sentry. What is left here is pulling data out of
Sentry objects and translating errors.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

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
        return f"{self.instance_url}/task/{key}"

    def get_issue_display_name(self, external_issue: Any) -> str:
        if external_issue.title:
            return f"{external_issue.key} — {external_issue.title}"
        return str(external_issue.key)

    def make_external_key(self, data: dict[str, Any]) -> str:
        return str(data["key"])

    def get_persisted_default_config_fields(self) -> Sequence[str]:
        # Abstract method of `IssueBasicIntegration`: without it the class stays abstract
        # and Sentry cannot create an installation. We do not persist form values — the
        # create cascade (`build_create_config`) does not read `get_defaults`.
        return []

    def _defaults_from_group(self, group: Group | None) -> tuple[str, str]:
        """Pre-fill the task title and description with Sentry issue data."""
        if group is None:
            return "", ""
        event = group.get_latest_event()
        # `IssueBasicIntegration.get_group_body` is not annotated in Sentry, and strict mode
        # forbids calling untyped functions. The ignore is only needed when checking against
        # the Sentry sources (see warn_unused_ignores for this module).
        body: str = (
            self.get_group_body(group, event)  # type: ignore[no-untyped-call]
            if event is not None
            else ""
        )
        sentry_url = absolute_uri(group.get_absolute_url(params={"referrer": "jaga_integration"}))
        description = build_description(sentry_url, group.culprit or "", body)
        return build_title(group.title), description

    def get_create_issue_config(
        self, group: Group | None, user: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        title, description = self._defaults_from_group(group)
        with _as_integration_error():
            return issue_config.build_create_config(
                self.get_client(), kwargs.get("params") or {}, title, description
            )

    def create_issue(self, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        with _as_integration_error():
            return issue_config.create_task_from_form(self.get_client(), data)

    def get_link_issue_config(self, group: Group, **kwargs: Any) -> list[dict[str, Any]]:
        with _as_integration_error():
            return issue_config.build_link_config(self.get_client(), kwargs.get("params") or {})

    def get_issue(self, issue_id: str, **kwargs: Any) -> dict[str, Any]:
        with _as_integration_error():
            return issue_config.get_task_summary(self.get_client(), issue_id)

    def search_issues(self, query: str | None, **kwargs: Any) -> list[dict[str, Any]]:
        with _as_integration_error():
            return issue_config.search_task_summaries(
                self.get_client(), kwargs.get("project_id"), query
            )
