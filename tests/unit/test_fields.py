import logging
from typing import Any

import pytest

from sentry_jaga.client.models import Attribute
from sentry_jaga.fields import (
    ASSIGNEE_OBJECT_TYPE,
    CREATE_TS_OBJECT_TYPE,
    CREATOR_OBJECT_TYPE,
    DESCRIPTION_OBJECT_TYPE,
    LABEL_OBJECT_TYPE,
    SPACE_OBJECT_TYPE,
    TITLE_OBJECT_TYPE,
    TYPE_OBJECT_TYPE,
    attribute_to_field,
    build_attribute_fields,
    extract_title,
    field_name,
    find_attribute,
    form_data_to_attributes,
    injected_attributes,
)


def _attr(attr_id: int, name: str, object_type: str, **kwargs: Any) -> Attribute:
    return Attribute(id=attr_id, name=name, object_type_name_m=object_type, **kwargs)


# The attribute set of a real Jaga task type ("Стандарт"), mnemonics and flags as the live
# instance reports them. The fixtures used to be invented, and that is exactly why nothing
# caught the missing space/type cells in the create payload.
SPACE = _attr(90, "Space", SPACE_OBJECT_TYPE, required=True)
TYPE = _attr(91, "Task type", TYPE_OBJECT_TYPE, required=True)
TITLE = _attr(100, "Title", TITLE_OBJECT_TYPE, required=True)
DESCRIPTION = _attr(101, "Description", DESCRIPTION_OBJECT_TYPE)
ASSIGNEE = _attr(103, "Assignees", ASSIGNEE_OBJECT_TYPE, multiple=True)
LABEL = _attr(104, "Label", LABEL_OBJECT_TYPE, multiple=True)
# No dictionaryId and no endpoint we could list it from: unsupported, dropped from the form.
PRIORITY = _attr(102, "Priority", "task.priority_id")
CREATOR = _attr(92, "Author", CREATOR_OBJECT_TYPE)
CREATE_TS = _attr(93, "Created at", CREATE_TS_OBJECT_TYPE)
# A dictionary-backed attribute (a custom one — Jaga gives it a `dictionaryId`).
SEVERITY = _attr(110, "Severity", "task.flex_severity", dictionary_id=55, required=True)
HIDDEN = _attr(105, "Internal", "task.flex_internal", dictionary_id=56, visible=False)

REAL_ATTRIBUTES = [SPACE, TYPE, TITLE, DESCRIPTION, ASSIGNEE, LABEL, PRIORITY, CREATOR, CREATE_TS]


def test_field_name_is_stable() -> None:
    """An ordinary attribute is named after its Jaga id — nothing outside this package could
    know what else to call it."""
    assert field_name(SEVERITY) == "attr_110"
    assert field_name(ASSIGNEE) == "attr_103"


def test_title_and_description_use_sentrys_canonical_field_names() -> None:
    """The two fields Sentry itself names must not hide behind an `attr_<id>` name.

    The alert-rule ticket action overwrites `data["title"]` / `data["description"]` with the
    title and body of the event that fired the rule, and its modal hides the fields by those
    exact names. Named `attr_100`, the title would never reach us, and every task filed by a
    rule would carry the empty default saved into the rule config. See CANONICAL_FIELD_NAMES.
    """
    assert field_name(TITLE) == "title"
    assert field_name(DESCRIPTION) == "description"


def test_find_attribute() -> None:
    assert find_attribute([TITLE, DESCRIPTION], TITLE_OBJECT_TYPE) is TITLE
    assert find_attribute([TITLE], "task.nope") is None


def test_attribute_to_field_title_is_required_string() -> None:
    field = attribute_to_field(TITLE, default="Login is broken")
    assert field["type"] == "string"
    assert field["required"] is True
    assert field["default"] == "Login is broken"


def test_attribute_to_field_description_is_textarea() -> None:
    field = attribute_to_field(DESCRIPTION, default="body")
    assert field["type"] == "textarea"
    assert field["default"] == "body"


def test_attribute_to_field_dictionary_is_select_with_choices() -> None:
    field = attribute_to_field(SEVERITY, choices=[("1", "High"), ("2", "Low")])
    assert field["type"] == "select"
    assert field["choices"] == [("1", "High"), ("2", "Low")]
    assert field["required"] is True
    assert "multiple" not in field


def test_attribute_to_field_assignee_is_multiple_select() -> None:
    """Assignees carry no `dictionaryId`, yet their values are person UUIDs, not free text."""
    field = attribute_to_field(ASSIGNEE, choices=[("uuid-1", "Ivanov")])
    assert field["type"] == "select"
    assert field["multiple"] is True
    assert field["choices"] == [("uuid-1", "Ivanov")]


def test_attribute_to_field_label_is_multiple_select() -> None:
    field = attribute_to_field(LABEL, choices=[("7", "backend")])
    assert field["type"] == "select"
    assert field["multiple"] is True
    assert field["choices"] == [("7", "backend")]


# --- what the form shows, and what it deliberately does not ----------------


def test_build_attribute_fields_renders_the_supported_attributes() -> None:
    fields = build_attribute_fields(
        attributes=[*REAL_ATTRIBUTES, SEVERITY],
        choices_by_dictionary={55: [("1", "High")]},
        title="Login is broken",
        description="Sentry issue: https://...",
        user_choices=[("uuid-1", "Ivanov")],
        label_choices=[("7", "backend")],
    )
    by_name = {f["name"]: f for f in fields}

    assert by_name["title"]["default"] == "Login is broken"
    assert by_name["description"]["default"] == "Sentry issue: https://..."
    assert by_name["attr_103"]["choices"] == [("uuid-1", "Ivanov")]
    assert by_name["attr_104"]["choices"] == [("7", "backend")]
    assert by_name["attr_110"]["choices"] == [("1", "High")]


def test_build_attribute_fields_hides_the_injected_and_server_attributes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The space and the type are submitted, never shown: the cascade selects already ask for
    them, and a second "Space" box in the same form would be a lie about what it controls.
    The author and the creation date are Jaga's to fill.

    They are DROPPED ON PURPOSE, not merely unsupported. Both distinctions matter: the space
    and the type are `required`, so letting them fall through to the "cannot render this" rule
    would log a warning blaming two fields that are, in fact, submitted correctly — noise aimed
    straight at whoever is debugging a failed create.
    """
    with caplog.at_level(logging.WARNING, logger="sentry_jaga.fields"):
        fields = build_attribute_fields(REAL_ATTRIBUTES, {}, "t", "d")

    names = [f["name"] for f in fields]
    assert field_name(SPACE) not in names
    assert field_name(TYPE) not in names
    assert field_name(CREATOR) not in names
    assert field_name(CREATE_TS) not in names

    assert "required_attribute_not_supported" not in caplog.text


def test_build_attribute_fields_skips_unsupported_reference_attribute() -> None:
    """Priority is a reference with no dictionary behind it: we have no list of its values, and
    a text box over an id column would only send Jaga garbage."""
    fields = build_attribute_fields([TITLE, PRIORITY], {}, "t", "d")
    assert [f["name"] for f in fields] == [field_name(TITLE)]


def test_build_attribute_fields_warns_when_a_skipped_attribute_is_required(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A required attribute we cannot render dooms the create. Jaga's own 500 tells the user
    which field is missing; this log line tells the operator that the plugin left it out."""
    required_priority = _attr(102, "Priority", "task.priority_id", required=True)

    with caplog.at_level(logging.WARNING, logger="sentry_jaga.fields"):
        build_attribute_fields([TITLE, required_priority], {}, "t", "d")

    assert "required_attribute_not_supported" in caplog.text
    record = caplog.records[0]
    assert record.object_type_name_m == "task.priority_id"  # type: ignore[attr-defined]
    assert record.attribute_name == "Priority"  # type: ignore[attr-defined]


def test_build_attribute_fields_stays_quiet_for_an_optional_skipped_attribute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="sentry_jaga.fields"):
        build_attribute_fields([TITLE, PRIORITY], {}, "t", "d")

    assert caplog.records == []


def test_build_attribute_fields_skips_hidden() -> None:
    fields = build_attribute_fields([TITLE, HIDDEN], {56: [("1", "x")]}, "t", "d")
    assert [f["name"] for f in fields] == [field_name(TITLE)]


def test_build_attribute_fields_orders_by_order_num() -> None:
    first = _attr(1, "Second", "task.flex_a", dictionary_id=1, order_num=2)
    second = _attr(2, "First", "task.flex_b", dictionary_id=2, order_num=1)
    fields = build_attribute_fields([first, second], {}, "t", "d")
    assert [f["name"] for f in fields] == ["attr_2", "attr_1"]


# --- the create payload ----------------------------------------------------


def test_form_data_to_attributes_builds_payload() -> None:
    payload = form_data_to_attributes(
        {"title": "Login is broken", "description": "body", "attr_110": "1", "project": "1"},
        [TITLE, DESCRIPTION, SEVERITY],
    )

    assert payload == [
        {"fieldId": 100, "value": "Login is broken", "referenceValue": False, "addInfo": {}},
        {"fieldId": 101, "value": "body", "referenceValue": False, "addInfo": {}},
        {
            "fieldId": 110,
            "value": "1",
            "referenceValue": True,
            "addInfo": {},
            "dictionaryId": 55,
        },
    ]


def test_form_data_to_attributes_marks_assignee_and_label_as_reference_values() -> None:
    """Neither has a `dictionaryId`, so the old rule ("reference iff dictionary") sent them as
    plain text. Their values are ids: Jaga needs `referenceValue` on them."""
    payload = form_data_to_attributes(
        {"attr_103": ["uuid-1"], "attr_104": ["7"]}, [ASSIGNEE, LABEL]
    )

    assert [item["referenceValue"] for item in payload] == [True, True]
    assert "dictionaryId" not in payload[0]  # they have none to send


def test_form_data_to_attributes_skips_empty_optional_values() -> None:
    payload = form_data_to_attributes(
        {"title": "Some title", "description": ""}, [TITLE, DESCRIPTION]
    )
    assert [item["fieldId"] for item in payload] == [100]


def test_form_data_to_attributes_keeps_multiple_values_as_list() -> None:
    payload = form_data_to_attributes({"attr_104": ["1", "2"]}, [LABEL])
    assert payload[0]["value"] == ["1", "2"]


def test_injected_attributes_carry_the_space_and_the_type() -> None:
    """Jaga refuses a create whose `attributes` omit these two — with an HTTP 500 — even though
    both ids are already in the create URL."""
    assert injected_attributes(REAL_ATTRIBUTES, project_id=7, type_id=33532) == [
        {"fieldId": 90, "value": 7, "referenceValue": True, "addInfo": {}},
        {"fieldId": 91, "value": 33532, "referenceValue": True, "addInfo": {}},
    ]


def test_injected_attributes_skips_what_the_task_type_does_not_declare() -> None:
    """A task type without the space attribute must not get a cell with a made-up fieldId."""
    assert injected_attributes([TITLE, TYPE], project_id=7, type_id=10) == [
        {"fieldId": 91, "value": 10, "referenceValue": True, "addInfo": {}}
    ]


def test_extract_title_from_raw_task() -> None:
    raw: dict[str, Any] = {
        "id": 5,
        "code": "PLT-5",
        "attributes": [
            {"fieldId": 101, "value": "body", "objectTypeNameM": DESCRIPTION_OBJECT_TYPE},
            {"fieldId": 100, "value": "Login is broken", "objectTypeNameM": TITLE_OBJECT_TYPE},
        ],
    }
    assert extract_title(raw) == "Login is broken"


def test_extract_title_missing_returns_code() -> None:
    assert extract_title({"code": "PLT-9", "attributes": []}) == "PLT-9"
