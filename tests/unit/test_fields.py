from typing import Any

from sentry_jaga.client.models import Attribute
from sentry_jaga.fields import (
    DESCRIPTION_OBJECT_TYPE,
    TITLE_OBJECT_TYPE,
    attribute_to_field,
    build_attribute_fields,
    extract_title,
    field_name,
    find_attribute,
    form_data_to_attributes,
)


def _attr(attr_id: int, name: str, object_type: str, **kwargs: Any) -> Attribute:
    return Attribute(id=attr_id, name=name, object_type_name_m=object_type, **kwargs)


TITLE = _attr(100, "Название", TITLE_OBJECT_TYPE, required=True)
DESCRIPTION = _attr(101, "Описание", DESCRIPTION_OBJECT_TYPE)
PRIORITY = _attr(102, "Приоритет", "task.priority", dictionary_id=55, required=True)
LABELS = _attr(103, "Метки", "task.labels", dictionary_id=56, multiple=True)
HIDDEN = _attr(104, "Служебное", "task.internal", visible=False)


def test_field_name_is_stable() -> None:
    assert field_name(TITLE) == "attr_100"


def test_find_attribute() -> None:
    assert find_attribute([TITLE, DESCRIPTION], TITLE_OBJECT_TYPE) is TITLE
    assert find_attribute([TITLE], "task.nope") is None


def test_attribute_to_field_plain_string() -> None:
    field = attribute_to_field(_attr(1, "Комментарий", "task.custom"))
    assert field["name"] == "attr_1"
    assert field["label"] == "Комментарий"
    assert field["type"] == "string"
    assert field["required"] is False


def test_attribute_to_field_title_is_required_string() -> None:
    field = attribute_to_field(TITLE, default="Падает логин")
    assert field["type"] == "string"
    assert field["required"] is True
    assert field["default"] == "Падает логин"


def test_attribute_to_field_description_is_textarea() -> None:
    field = attribute_to_field(DESCRIPTION, default="тело")
    assert field["type"] == "textarea"
    assert field["default"] == "тело"


def test_attribute_to_field_dictionary_is_select_with_choices() -> None:
    field = attribute_to_field(PRIORITY, choices=[("1", "Высокий"), ("2", "Низкий")])
    assert field["type"] == "select"
    assert field["choices"] == [("1", "Высокий"), ("2", "Низкий")]
    assert field["required"] is True
    assert "multiple" not in field


def test_attribute_to_field_multiple_dictionary() -> None:
    field = attribute_to_field(LABELS, choices=[("1", "bug")])
    assert field["type"] == "select"
    assert field["multiple"] is True


def test_build_attribute_fields_skips_hidden_and_prefills_system() -> None:
    fields = build_attribute_fields(
        attributes=[TITLE, DESCRIPTION, PRIORITY, HIDDEN],
        choices_by_dictionary={55: [("1", "Высокий")]},
        title="Падает логин",
        description="Sentry-issue: https://...",
    )
    names = [f["name"] for f in fields]
    assert names == ["attr_100", "attr_101", "attr_102"]
    assert "attr_104" not in names

    by_name = {f["name"]: f for f in fields}
    assert by_name["attr_100"]["default"] == "Падает логин"
    assert by_name["attr_101"]["default"] == "Sentry-issue: https://..."
    assert by_name["attr_102"]["choices"] == [("1", "Высокий")]


def test_form_data_to_attributes_builds_payload() -> None:
    form_data = {
        "attr_100": "Падает логин",
        "attr_101": "тело",
        "attr_102": "1",
        "project": "1",
        "issue_type": "10",
    }
    payload = form_data_to_attributes(form_data, [TITLE, DESCRIPTION, PRIORITY])

    assert payload == [
        {"fieldId": 100, "value": "Падает логин", "referenceValue": False, "addInfo": {}},
        {"fieldId": 101, "value": "тело", "referenceValue": False, "addInfo": {}},
        {
            "fieldId": 102,
            "value": "1",
            "referenceValue": True,
            "addInfo": {},
            "dictionaryId": 55,
        },
    ]


def test_form_data_to_attributes_skips_empty_optional_values() -> None:
    payload = form_data_to_attributes(
        {"attr_100": "Заголовок", "attr_101": ""}, [TITLE, DESCRIPTION]
    )
    assert [item["fieldId"] for item in payload] == [100]


def test_form_data_to_attributes_keeps_multiple_values_as_list() -> None:
    payload = form_data_to_attributes({"attr_103": ["1", "2"]}, [LABELS])
    assert payload[0]["value"] == ["1", "2"]
    assert payload[0]["referenceValue"] is True


def test_extract_title_from_raw_task() -> None:
    raw: dict[str, Any] = {
        "id": 5,
        "code": "PLT-5",
        "attributes": [
            {"fieldId": 101, "value": "тело", "objectTypeNameM": DESCRIPTION_OBJECT_TYPE},
            {"fieldId": 100, "value": "Падает логин", "objectTypeNameM": TITLE_OBJECT_TYPE},
        ],
    }
    assert extract_title(raw) == "Падает логин"


def test_extract_title_missing_returns_code() -> None:
    assert extract_title({"code": "PLT-9", "attributes": []}) == "PLT-9"
