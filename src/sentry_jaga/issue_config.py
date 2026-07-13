"""Логика issue-слоя: сборка форм Sentry и операции над задачами Яги.

Модуль framework-agnostic — НЕ импортирует `sentry`. Классы слоя интеграции
(`issues.py`, `sync.py`) — тонкие делегаты поверх этих функций. Так вся реальная
логика покрывается юнит-тестами без тестового стека Sentry (Postgres/Kafka/Snuba).

Формат field-dict — тот, что понимает фронтенд Sentry (`ExternalIssueForm`):
name, label, type (string|textarea|select), default, choices, required,
multiple, updatesForm, help.
"""

from __future__ import annotations

from typing import Any

from sentry_jaga.client.api import JagaClient
from sentry_jaga.client.exceptions import JagaError
from sentry_jaga.client.models import Project
from sentry_jaga.fields import (
    TITLE_OBJECT_TYPE,
    build_attribute_fields,
    extract_title,
    field_name,
    find_attribute,
    form_data_to_attributes,
)

SEARCH_LIMIT = 20
RESOLVED_COMMENT = "Sentry-issue закрыт. Задача может быть завершена."
UNRESOLVED_COMMENT = "Sentry-issue переоткрыт: ошибка воспроизвелась снова."


class NoProjectsError(JagaError):
    """У сервисного аккаунта нет доступных пространств в Яге."""


def _project_choices(projects: list[Project]) -> list[tuple[str, str]]:
    return [(str(p.id), f"{p.title} ({p.code})") for p in projects]


def _selected_id(params: dict[str, Any], key: str, default: int) -> int:
    raw = params.get(key)
    return int(raw) if raw else default


def _require_projects(client: JagaClient) -> list[Project]:
    projects = client.get_projects()
    if not projects:
        raise NoProjectsError("В Яге нет доступных пространств для сервисного аккаунта.")
    return projects


def _project_field(projects: list[Project], project_id: int) -> dict[str, Any]:
    return {
        "name": "project",
        "label": "Пространство",
        "type": "select",
        "choices": _project_choices(projects),
        "default": str(project_id),
        "required": True,
        "updatesForm": True,
    }


def build_create_config(
    client: JagaClient, params: dict[str, Any], title: str, description: str
) -> list[dict[str, Any]]:
    """Каскад формы создания: пространство → тип задачи → динамические атрибуты."""
    projects = _require_projects(client)
    project_id = _selected_id(params, "project", projects[0].id)
    fields: list[dict[str, Any]] = [_project_field(projects, project_id)]

    task_types = client.get_task_types(project_id)
    if not task_types:
        return fields

    type_id = _selected_id(params, "issue_type", task_types[0].id)
    fields.append(
        {
            "name": "issue_type",
            "label": "Тип задачи",
            "type": "select",
            "choices": [(str(t.id), t.name) for t in task_types],
            "default": str(type_id),
            "required": True,
            "updatesForm": True,
        }
    )

    attributes = client.get_task_type_attributes(project_id, type_id)
    choices_by_dictionary = {
        attr.dictionary_id: client.get_dictionary_values(attr.dictionary_id)
        for attr in attributes
        if attr.dictionary_id is not None and attr.visible
    }
    fields.extend(build_attribute_fields(attributes, choices_by_dictionary, title, description))
    return fields


def create_task_from_form(client: JagaClient, form_data: dict[str, Any]) -> dict[str, Any]:
    """Создать задачу Яги по данным формы Sentry."""
    project_id = int(form_data["project"])
    type_id = int(form_data["issue_type"])

    attributes = client.get_task_type_attributes(project_id, type_id)
    payload = form_data_to_attributes(form_data, attributes)
    if not payload:
        raise JagaError("Не заполнено ни одного атрибута задачи.")

    task = client.create_task(project_id, type_id, payload)

    title_attr = find_attribute(attributes, TITLE_OBJECT_TYPE)
    title = str(form_data.get(field_name(title_attr), "")) if title_attr else task.code
    return {"key": task.code, "title": title, "description": ""}


def build_link_config(client: JagaClient, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Поля формы линковки: пространство, строка поиска, выбор задачи.

    Живого autocomplete нет — внешний пакет не может зарегистрировать search-endpoint
    в urlconf Sentry. Поэтому поиск идёт через `updatesForm`: пользователь вводит
    запрос, форма перезапрашивается, список задач наполняется.
    """
    projects = _require_projects(client)
    project_id = _selected_id(params, "project", projects[0].id)
    query = str(params.get("query") or "")

    choices: list[tuple[str, str]] = []
    if query:
        choices = [
            (task.code, f"{task.code} — {task.title}")
            for task in client.search_tasks(project_id, query, size=SEARCH_LIMIT)
        ]

    return [
        _project_field(projects, project_id),
        {
            "name": "query",
            "label": "Поиск задачи",
            "type": "string",
            "default": query,
            "required": False,
            "updatesForm": True,
            "help": "Введите код или часть названия задачи и обновите список ниже.",
        },
        {
            "name": "externalIssue",
            "label": "Задача",
            "type": "select",
            "choices": choices,
            "required": True,
            "help": "Если список пуст — уточните поисковый запрос выше.",
        },
    ]


def get_task_summary(client: JagaClient, code: str) -> dict[str, Any]:
    """Сводка по задаче Яги для `ExternalIssue` Sentry."""
    raw = client.get_task_by_code(code)
    return {
        "key": raw["code"],
        "title": extract_title(raw),
        "description": "",
        "metadata": {"task_id": raw["id"]},
    }


def search_task_summaries(
    client: JagaClient, project_id: int | None, query: str | None
) -> list[dict[str, Any]]:
    if not project_id or not query:
        return []
    return [
        {"key": task.code, "title": task.title}
        for task in client.search_tasks(int(project_id), query, size=SEARCH_LIMIT)
    ]


def status_comment(is_resolved: bool) -> str:
    return RESOLVED_COMMENT if is_resolved else UNRESOLVED_COMMENT


def resolve_task_id(client: JagaClient, code: str, metadata: dict[str, Any] | None) -> int:
    """ID задачи: из метаданных `ExternalIssue`, иначе — поиск по коду."""
    task_id = (metadata or {}).get("task_id")
    if task_id:
        return int(task_id)
    raw = client.get_task_by_code(code)
    return int(raw["id"])
