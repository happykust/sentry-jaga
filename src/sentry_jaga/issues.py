"""Issue-слой Sentry: тонкий делегат поверх `sentry_jaga.issue_config`.

Вся логика (сборка полей, конвертация формы, поиск) живёт в ядре и покрыта
юнит-тестами без Sentry. Здесь — только извлечение данных из объектов Sentry
и трансляция ошибок.
"""

from __future__ import annotations

from collections.abc import Iterator
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
    """Перевести ошибки ядра в исключения, которые понимает Sentry."""
    try:
        yield
    except JagaNotFoundError as exc:
        raise IntegrationFormError({"externalIssue": str(exc)}) from exc
    except JagaError as exc:
        raise IntegrationError(str(exc)) from exc


class JagaIssuesMixin(IssueBasicIntegration):
    """Реализация issue-контракта Sentry поверх ядра."""

    def get_client(self) -> JagaClient:  # переопределяется в JagaIntegration
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

    def _defaults_from_group(self, group: Group | None) -> tuple[str, str]:
        """Предзаполнение названия и описания задачи данными Sentry-issue."""
        if group is None:
            return "", ""
        event = group.get_latest_event()
        # `IssueBasicIntegration.get_group_body` в Sentry не аннотирован, а strict
        # запрещает вызовы нетипизированных функций. Ignore нужен только при проверке
        # против исходников Sentry (см. warn_unused_ignores для этого модуля).
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
