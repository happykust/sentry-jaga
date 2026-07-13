"""Маппинг EAV-атрибутов Яги в поля формы Sentry и обратно.

Sentry рендерит формы create/link по списку field-dict, который возвращает
интеграция. Поддерживаемые ключи: name, label, type (string/textarea/select),
default, choices, required, multiple, updatesForm, help.

Яга описывает поля задачи атрибутами (EAV). Системные атрибуты опознаются
по мнемокоду `objectTypeNameM` (например, `task.title`).
"""

from __future__ import annotations

from typing import Any

from sentry_jaga.client.models import Attribute

TITLE_OBJECT_TYPE = "task.title"
DESCRIPTION_OBJECT_TYPE = "task.content_data"

FIELD_PREFIX = "attr_"


def field_name(attr: Attribute) -> str:
    """Имя поля формы Sentry для атрибута Яги."""
    return f"{FIELD_PREFIX}{attr.id}"


def find_attribute(attributes: list[Attribute], object_type: str) -> Attribute | None:
    """Найти атрибут по мнемокоду системного типа."""
    for attr in attributes:
        if attr.object_type_name_m == object_type:
            return attr
    return None


def attribute_to_field(
    attr: Attribute,
    choices: list[tuple[str, str]] | None = None,
    default: Any = None,
) -> dict[str, Any]:
    """Превратить определение атрибута Яги в field-dict Sentry."""
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
    """Собрать поля формы для всех видимых атрибутов типа задачи.

    Системные атрибуты «название» и «описание» предзаполняются данными Sentry.
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
    """Собрать `attributes` для создания задачи из данных формы Sentry."""
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
    """Достать название задачи из ответа Яги; фолбэк — код задачи."""
    for raw in raw_task.get("attributes", []):
        if raw.get("objectTypeNameM") == TITLE_OBJECT_TYPE:
            value = raw.get("value")
            if isinstance(value, str) and value:
                return value
    code = raw_task.get("code", "")
    return str(code)
