from typing import Any

import pytest

from sentry_jaga.client.exceptions import JagaError
from sentry_jaga.client.models import Attribute, Project, TaskRef, TaskType
from sentry_jaga.issue_config import (
    NoProjectsError,
    build_create_config,
    build_link_config,
    create_task_from_form,
    get_task_summary,
    resolve_task_id,
    search_task_summaries,
    status_comment,
)

TITLE = Attribute(id=100, name="Название", object_type_name_m="task.title", required=True)
DESCRIPTION = Attribute(id=101, name="Описание", object_type_name_m="task.content_data")
PRIORITY = Attribute(
    id=102, name="Приоритет", object_type_name_m="task.priority", dictionary_id=55, required=True
)


class FakeClient:
    """Подставной JagaClient: те же методы, записывает вызовы."""

    def __init__(
        self,
        projects: list[Project] | None = None,
        task_types: list[TaskType] | None = None,
        attributes: list[Attribute] | None = None,
    ) -> None:
        self._projects = projects if projects is not None else [Project(1, "Платформа", "PLT")]
        self._task_types = task_types if task_types is not None else [TaskType(10, "Баг")]
        self._attributes = attributes if attributes is not None else [TITLE, DESCRIPTION, PRIORITY]
        self.created: dict[str, Any] | None = None
        self.comments: list[tuple[int, str]] = []

    def get_projects(self) -> list[Project]:
        return self._projects

    def get_task_types(self, project_id: int) -> list[TaskType]:
        return self._task_types

    def get_task_type_attributes(self, project_id: int, task_type_id: int) -> list[Attribute]:
        return self._attributes

    def get_dictionary_values(self, dictionary_id: int) -> list[tuple[str, str]]:
        return [("1", "Высокий"), ("2", "Низкий")]

    def create_task(
        self, project_id: int, task_type_id: int, attributes: list[dict[str, Any]]
    ) -> TaskRef:
        self.created = {
            "project_id": project_id,
            "task_type_id": task_type_id,
            "attributes": attributes,
        }
        return TaskRef(id=500, code="PLT-500", title="")

    def get_task_by_code(self, code: str) -> dict[str, Any]:
        return {
            "id": 500,
            "code": code,
            "attributes": [
                {"fieldId": 100, "value": "Падает логин", "objectTypeNameM": "task.title"}
            ],
        }

    def search_tasks(self, project_id: int, text: str, *, size: int = 20) -> list[TaskRef]:
        return [TaskRef(id=5, code="PLT-5", title="Падает логин")]

    def create_comment(self, task_id: int, content: str) -> None:
        self.comments.append((task_id, content))


def _by_name(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in fields}


def test_create_config_builds_cascade() -> None:
    fields = build_create_config(FakeClient(), {}, "Падает логин", "Sentry-issue: http://s/1")
    by_name = _by_name(fields)

    assert by_name["project"]["updatesForm"] is True
    assert by_name["project"]["choices"] == [("1", "Платформа (PLT)")]
    assert by_name["issue_type"]["updatesForm"] is True
    assert by_name["issue_type"]["choices"] == [("10", "Баг")]
    assert by_name["attr_100"]["default"] == "Падает логин"
    assert by_name["attr_101"]["default"] == "Sentry-issue: http://s/1"
    assert by_name["attr_102"]["choices"] == [("1", "Высокий"), ("2", "Низкий")]


def test_create_config_honours_selected_params() -> None:
    client = FakeClient(
        projects=[Project(1, "Платформа", "PLT"), Project(2, "Биллинг", "BIL")],
        task_types=[TaskType(10, "Баг"), TaskType(11, "Задача")],
    )
    fields = build_create_config(client, {"project": "2", "issue_type": "11"}, "t", "d")
    by_name = _by_name(fields)

    assert by_name["project"]["default"] == "2"
    assert by_name["issue_type"]["default"] == "11"


def test_create_config_without_projects_raises() -> None:
    with pytest.raises(NoProjectsError):
        build_create_config(FakeClient(projects=[]), {}, "t", "d")


def test_create_config_without_task_types_stops_at_project() -> None:
    fields = build_create_config(FakeClient(task_types=[]), {}, "t", "d")
    assert [f["name"] for f in fields] == ["project"]


def test_create_task_from_form_sends_attributes() -> None:
    client = FakeClient()
    result = create_task_from_form(
        client,
        {
            "project": "1",
            "issue_type": "10",
            "attr_100": "Падает логин",
            "attr_101": "тело",
            "attr_102": "1",
        },
    )

    assert result["key"] == "PLT-500"
    assert result["title"] == "Падает логин"
    assert client.created is not None
    assert [a["fieldId"] for a in client.created["attributes"]] == [100, 101, 102]
    assert client.created["project_id"] == 1
    assert client.created["task_type_id"] == 10


def test_create_task_from_form_rejects_empty_form() -> None:
    with pytest.raises(JagaError):
        create_task_from_form(FakeClient(), {"project": "1", "issue_type": "10"})


def test_link_config_searches_when_query_given() -> None:
    fields = build_link_config(FakeClient(), {"project": "1", "query": "логин"})
    by_name = _by_name(fields)

    assert by_name["query"]["updatesForm"] is True
    assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Падает логин")]


def test_link_config_without_query_has_no_choices() -> None:
    by_name = _by_name(build_link_config(FakeClient(), {"project": "1"}))
    assert by_name["externalIssue"]["choices"] == []


def test_get_task_summary() -> None:
    summary = get_task_summary(FakeClient(), "PLT-500")
    assert summary["key"] == "PLT-500"
    assert summary["title"] == "Падает логин"
    assert summary["metadata"] == {"task_id": 500}


def test_search_task_summaries() -> None:
    assert search_task_summaries(FakeClient(), 1, "логин") == [
        {"key": "PLT-5", "title": "Падает логин"}
    ]


def test_search_task_summaries_without_query_is_empty() -> None:
    assert search_task_summaries(FakeClient(), 1, "") == []


def test_status_comment_distinguishes_resolution() -> None:
    assert "закрыт" in status_comment(is_resolved=True).lower()
    assert "переоткрыт" in status_comment(is_resolved=False).lower()


def test_resolve_task_id_prefers_metadata() -> None:
    assert resolve_task_id(FakeClient(), "PLT-500", {"task_id": 77}) == 77


def test_resolve_task_id_falls_back_to_lookup_by_code() -> None:
    assert resolve_task_id(FakeClient(), "PLT-500", {}) == 500
