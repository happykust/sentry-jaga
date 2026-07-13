"""Issue-layer logic: building Sentry forms and operating on Jaga tasks.

This module is framework-agnostic — it does NOT import `sentry`. The integration-layer
classes (`issues.py`, `sync.py`) are thin delegates on top of these functions. That way
all the real logic is covered by unit tests without Sentry's test stack
(Postgres/Kafka/Snuba).

The field-dict format is the one Sentry's frontend understands (`ExternalIssueForm`):
name, label, type (string|textarea|select), default, choices, required,
multiple, updatesForm, help.
"""

from __future__ import annotations

import logging
from typing import Any

from sentry_jaga.client.api import JagaClient
from sentry_jaga.client.exceptions import JagaError
from sentry_jaga.client.models import Attribute, Project
from sentry_jaga.fields import (
    DESCRIPTION_OBJECT_TYPE,
    TITLE_OBJECT_TYPE,
    build_attribute_fields,
    extract_title,
    field_name,
    find_attribute,
    form_data_to_attributes,
)

logger = logging.getLogger("sentry_jaga.issue_config")

SEARCH_LIMIT = 20
# `updatesForm` makes Sentry's frontend re-fetch the config on every keystroke. There is
# nowhere to hook up a debounce of our own, so we damp the noise with a minimum query
# length instead.
MIN_QUERY_LENGTH = 3
RESOLVED_COMMENT = "The Sentry issue has been resolved. This task can be completed."
UNRESOLVED_COMMENT = "The Sentry issue has been reopened: the error happened again."


class NoProjectsError(JagaError):
    """The service account has no spaces available in Jaga."""


def _project_choices(projects: list[Project]) -> list[tuple[str, str]]:
    return [(str(p.id), f"{p.title} ({p.code})") for p in projects]


def _selected_id(params: dict[str, Any], key: str, available: list[int]) -> int:
    """The id selected in `params` — but only if it is among the available ones.

    With `updatesForm`, Sentry's frontend resends EVERY form field, not just the one that
    changed. After the space is switched, `params` still carries the `issue_type` of the
    previous one: taking it at face value means a 404 from Jaga or, worse, silently
    creating a task whose type belongs to another space. So the value is validated against
    the current list, and on a miss we fall back to the first available one.

    `available` is never empty: the caller guarantees that.
    """
    raw: Any = params.get(key)
    try:
        candidate = int(raw)
    except (TypeError, ValueError):  # key absent (None), empty, or not a number
        return available[0]
    return candidate if candidate in available else available[0]


def _require_projects(client: JagaClient) -> list[Project]:
    projects = client.get_projects()
    if not projects:
        raise NoProjectsError("This service account has no spaces available in Jaga.")
    return projects


def _warn_if_no_system_attributes(
    attributes: list[Attribute], project_id: int, type_id: int
) -> None:
    """Warn if not a single system attribute was recognised on the task type.

    The mnemonic codes `task.title` / `task.content_data` were inferred from the pattern
    `task.<snake_case_column>` (the Jaga spec only confirms `task.mcode`, `task.creator_id`
    and `task.project_id`) and are not confirmed by the docs themselves. If we guessed
    wrong, the form will quietly come out without a title and a description — and the
    Sentry context will never reach the task. Let the miss at least be visible in the logs.
    """
    if find_attribute(attributes, TITLE_OBJECT_TYPE) or find_attribute(
        attributes, DESCRIPTION_OBJECT_TYPE
    ):
        return
    logger.warning(
        "jaga.issue_config.system_attributes_not_found",
        extra={
            "project_id": project_id,
            "task_type_id": type_id,
            "expected": [TITLE_OBJECT_TYPE, DESCRIPTION_OBJECT_TYPE],
            "seen": [attr.object_type_name_m for attr in attributes],
        },
    )


def _project_field(projects: list[Project], project_id: int) -> dict[str, Any]:
    return {
        "name": "project",
        "label": "Space",
        "type": "select",
        "choices": _project_choices(projects),
        "default": str(project_id),
        "required": True,
        "updatesForm": True,
    }


def build_create_config(
    client: JagaClient, params: dict[str, Any], title: str, description: str
) -> list[dict[str, Any]]:
    """The create-form cascade: space -> task type -> dynamic attributes."""
    projects = _require_projects(client)
    project_id = _selected_id(params, "project", [p.id for p in projects])
    fields: list[dict[str, Any]] = [_project_field(projects, project_id)]

    task_types = client.get_task_types(project_id)
    if not task_types:
        return fields

    # Types are validated against the list of the CURRENT space: see `_selected_id`.
    type_id = _selected_id(params, "issue_type", [t.id for t in task_types])
    fields.append(
        {
            "name": "issue_type",
            "label": "Task type",
            "type": "select",
            "choices": [(str(t.id), t.name) for t in task_types],
            "default": str(type_id),
            "required": True,
            "updatesForm": True,
        }
    )

    attributes = client.get_task_type_attributes(project_id, type_id)
    _warn_if_no_system_attributes(attributes, project_id, type_id)
    choices_by_dictionary = {
        attr.dictionary_id: client.get_dictionary_values(attr.dictionary_id)
        for attr in attributes
        if attr.dictionary_id is not None and attr.visible
    }
    fields.extend(build_attribute_fields(attributes, choices_by_dictionary, title, description))
    return fields


def create_task_from_form(client: JagaClient, form_data: dict[str, Any]) -> dict[str, Any]:
    """Create a Jaga task from the data submitted in a Sentry form."""
    project_id = int(form_data["project"])
    type_id = int(form_data["issue_type"])

    attributes = client.get_task_type_attributes(project_id, type_id)
    payload = form_data_to_attributes(form_data, attributes)
    if not payload:
        raise JagaError("Not a single task attribute was filled in.")

    task = client.create_task(project_id, type_id, payload)

    title_attr = find_attribute(attributes, TITLE_OBJECT_TYPE)
    title = str(form_data.get(field_name(title_attr), "")) if title_attr else task.code
    # `metadata` travels into `ExternalIssue`. Without `task_id`, every resolve would look
    # the task up by code again (`GET /v1/task/findExtendedWithFlexField/code/{code}`) —
    # even though Jaga has just returned the id itself. Cf. `get_task_summary`.
    return {
        "key": task.code,
        "title": title,
        "description": "",
        "metadata": {"task_id": task.id},
    }


def build_link_config(client: JagaClient, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Fields of the link form: space, search query, task picker.

    There is no live autocomplete — an external package cannot register a search endpoint
    in Sentry's urlconf. So the search runs through `updatesForm`: the user types a query,
    the form is re-fetched, and the task list fills up.

    The flip side of `updatesForm` is that Sentry's frontend re-fetches the config on every
    keystroke, there is no debounce there, and an external package cannot ship its own JS.
    We soften it on the server: we do not search for queries shorter than
    `MIN_QUERY_LENGTH`, and the list of spaces is served from the client's cache (see
    `JagaClient.get_projects`).
    """
    projects = _require_projects(client)
    project_id = _selected_id(params, "project", [p.id for p in projects])
    query = str(params.get("query") or "").strip()

    choices: list[tuple[str, str]] = []
    if len(query) >= MIN_QUERY_LENGTH:
        choices = [
            (task.code, f"{task.code} — {task.title}")
            for task in client.search_tasks(project_id, query, size=SEARCH_LIMIT)
        ]

    return [
        _project_field(projects, project_id),
        {
            "name": "query",
            "label": "Task search",
            "type": "string",
            "default": query,
            "required": False,
            "updatesForm": True,
            "help": (
                f"Enter a task code or part of a task title — the search starts "
                f"at {MIN_QUERY_LENGTH} characters."
            ),
        },
        {
            "name": "externalIssue",
            "label": "Task",
            "type": "select",
            "choices": choices,
            "required": True,
            "help": "If the list is empty, refine the search query above.",
        },
    ]


def get_task_summary(client: JagaClient, code: str) -> dict[str, Any]:
    """Summary of a Jaga task for Sentry's `ExternalIssue`."""
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
    """Task id: from the `ExternalIssue` metadata, otherwise looked up by code."""
    task_id = (metadata or {}).get("task_id")
    if task_id:
        return int(task_id)
    raw = client.get_task_by_code(code)
    return int(raw["id"])
