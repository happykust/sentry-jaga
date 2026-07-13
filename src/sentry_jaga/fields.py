"""Mapping of Jaga EAV attributes to Sentry form fields and back.

Sentry renders the create/link forms from a list of field dicts returned by the
integration. Supported keys: name, label, type (string/textarea/select), default,
choices, required, multiple, updatesForm, help.

Jaga describes the fields of a task with attributes (EAV). System attributes are
recognised by their mnemonic code `objectTypeNameM` (for example, `task.title`).
"""

from __future__ import annotations

from typing import Any

from sentry_jaga.client.models import Attribute

TITLE_OBJECT_TYPE = "task.title"
DESCRIPTION_OBJECT_TYPE = "task.content_data"

FIELD_PREFIX = "attr_"


def field_name(attr: Attribute) -> str:
    """Name of the Sentry form field for a Jaga attribute."""
    return f"{FIELD_PREFIX}{attr.id}"


def find_attribute(attributes: list[Attribute], object_type: str) -> Attribute | None:
    """Find an attribute by the mnemonic code of its system type."""
    for attr in attributes:
        if attr.object_type_name_m == object_type:
            return attr
    return None


def attribute_to_field(
    attr: Attribute,
    choices: list[tuple[str, str]] | None = None,
    default: Any = None,
) -> dict[str, Any]:
    """Turn a Jaga attribute definition into a Sentry field dict."""
    field: dict[str, Any] = {
        "name": field_name(attr),
        "label": attr.name,
        "required": attr.required,
    }

    if attr.dictionary_id is not None:
        field["type"] = "select"
        field["choices"] = choices or []
        if attr.multiple:
            field["multiple"] = True
    elif attr.object_type_name_m == DESCRIPTION_OBJECT_TYPE:
        field["type"] = "textarea"
        field["autosize"] = True
        field["maxRows"] = 10
    else:
        field["type"] = "string"

    if default is not None:
        field["default"] = default
    return field


def build_attribute_fields(
    attributes: list[Attribute],
    choices_by_dictionary: dict[int, list[tuple[str, str]]],
    title: str,
    description: str,
) -> list[dict[str, Any]]:
    """Build the form fields for every visible attribute of a task type.

    The system attributes "title" and "description" are pre-filled with Sentry data.
    """
    fields: list[dict[str, Any]] = []
    for attr in sorted(attributes, key=lambda a: a.order_num):
        if not attr.visible:
            continue
        default: Any = None
        if attr.object_type_name_m == TITLE_OBJECT_TYPE:
            default = title
        elif attr.object_type_name_m == DESCRIPTION_OBJECT_TYPE:
            default = description
        choices = (
            choices_by_dictionary.get(attr.dictionary_id, [])
            if attr.dictionary_id is not None
            else None
        )
        fields.append(attribute_to_field(attr, choices=choices, default=default))
    return fields


def form_data_to_attributes(
    form_data: dict[str, Any], attributes: list[Attribute]
) -> list[dict[str, Any]]:
    """Build the `attributes` payload for task creation from Sentry form data."""
    payload: list[dict[str, Any]] = []
    for attr in attributes:
        value = form_data.get(field_name(attr))
        if value is None or value == "" or value == []:
            continue
        item: dict[str, Any] = {
            "fieldId": attr.id,
            "value": value,
            "referenceValue": attr.dictionary_id is not None,
            "addInfo": {},
        }
        if attr.dictionary_id is not None:
            item["dictionaryId"] = attr.dictionary_id
        payload.append(item)
    return payload


def extract_title(raw_task: dict[str, Any]) -> str:
    """Pull the task title out of a Jaga response; fall back to the task code."""
    for raw in raw_task.get("attributes", []):
        if raw.get("objectTypeNameM") == TITLE_OBJECT_TYPE:
            value = raw.get("value")
            if isinstance(value, str) and value:
                return value
    code = raw_task.get("code", "")
    return str(code)
