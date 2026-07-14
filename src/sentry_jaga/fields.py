"""Mapping of Jaga EAV attributes to Sentry form fields and back.

Sentry renders the create/link forms from a list of field dicts returned by the
integration. Supported keys: name, label, type (string/textarea/select), default,
choices, required, multiple, updatesForm, help.

Jaga describes the fields of a task with attributes (EAV). System attributes are
recognised by their mnemonic code `objectTypeNameM` (for example, `task.task_title`).

Which attributes we render, and why the rest are dropped
--------------------------------------------------------
The task-type endpoint hands back `AttributeApiDto`, which carries no data type — only a
mnemonic, an optional `dictionaryId`, and the display flags. So an attribute is renderable
only when we can tell what its values *are*:

* `task.task_title` -> `string`, pre-filled with the Sentry issue title;
* `task.content` -> `textarea`, pre-filled with the Sentry context;
* anything with a `dictionaryId` -> `select` over `/v1/listRef/{id}/any`;
* `task.assignee_uuid` -> `select` over the members of the space (values are person UUIDs);
* `task.label_id` -> `select` over the labels (values are label ids).

`SPACE_OBJECT_TYPE` / `TYPE_OBJECT_TYPE` are a special case: Jaga rejects a create that omits
them from `attributes` (HTTP 500, "Поле "Пространство" обязательно для заполнения") even
though both ids are already in the create URL. Rendering them would duplicate the cascade
selects, so they are hidden from the form and injected into the payload at submit time — see
`injected_attributes`.

`SERVER_OBJECT_TYPES` (author, creation date) are filled in by Jaga itself: neither rendered
nor sent.

Everything else — priority, release, parent, estimate, deadlines, work periods, and any
unrecognised attribute — is a reference or a typed value whose choices we cannot source. A
plain text box over an id column only yields garbage that Jaga rejects, so those attributes
are skipped; the user fills them in Jaga afterwards. When a skipped attribute is `required`,
the create WILL fail with Jaga's own message, and the warning logged here names the culprit.
"""

from __future__ import annotations

import logging
from typing import Any

from sentry_jaga.client.models import Attribute

logger = logging.getLogger("sentry_jaga.fields")

TITLE_OBJECT_TYPE = "task.task_title"
DESCRIPTION_OBJECT_TYPE = "task.content"
SPACE_OBJECT_TYPE = "task.project_id"
TYPE_OBJECT_TYPE = "task.type_id"
ASSIGNEE_OBJECT_TYPE = "task.assignee_uuid"
LABEL_OBJECT_TYPE = "task.label_id"
CREATOR_OBJECT_TYPE = "task.creator_id"
CREATE_TS_OBJECT_TYPE = "task.create_ts"

# Hidden from the form, but mandatory in the payload: their value comes from the cascade.
INJECTED_OBJECT_TYPES = frozenset({SPACE_OBJECT_TYPE, TYPE_OBJECT_TYPE})

# Jaga sets these itself on create. Neither rendered nor sent.
SERVER_OBJECT_TYPES = frozenset({CREATOR_OBJECT_TYPE, CREATE_TS_OBJECT_TYPE})

# Reference attributes with no `dictionaryId` whose values we know how to fetch anyway.
SOURCED_OBJECT_TYPES = frozenset({ASSIGNEE_OBJECT_TYPE, LABEL_OBJECT_TYPE})

FIELD_PREFIX = "attr_"

# Sentry's own names for the two fields every issue-tracking integration has; see
# `IssueBasicIntegration.get_create_issue_config`, whose default form is exactly
# `title` + `description`.
#
# Using them for the Jaga title/description attributes instead of the generic `attr_<id>`
# is NOT cosmetic — the alert-rule ticket action depends on these names:
#
# * `sentry.rules.actions.integrations.create_ticket.utils.create_issue` overwrites
#   `data["title"]` / `data["description"]` with the title and body of the event that fired
#   the rule, then hands `data` to `create_issue()`. Under an `attr_<id>` name we would never
#   see those values, and every rule-created task would carry the empty default that was
#   saved into the rule config (`get_create_issue_config` is called with `group=None` there).
# * The ticket-rule modal hides fields named `title`/`description` and shows "This will be
#   the same as the Sentry Issue." in their place. An `attr_<id>` field would instead be
#   rendered as an editable box whose value the rule then silently ignores.
CANONICAL_FIELD_NAMES = {
    TITLE_OBJECT_TYPE: "title",
    DESCRIPTION_OBJECT_TYPE: "description",
}


def field_name(attr: Attribute) -> str:
    """Name of the Sentry form field for a Jaga attribute.

    The title and the description travel under Sentry's canonical names; everything else is
    named after the Jaga attribute id, because nothing outside this package knows what it is.
    """
    canonical = CANONICAL_FIELD_NAMES.get(attr.object_type_name_m)
    if canonical is not None:
        return canonical
    return f"{FIELD_PREFIX}{attr.id}"


def find_attribute(attributes: list[Attribute], object_type: str) -> Attribute | None:
    """Find an attribute by the mnemonic code of its system type."""
    for attr in attributes:
        if attr.object_type_name_m == object_type:
            return attr
    return None


def is_reference(attr: Attribute) -> bool:
    """Does the attribute's value travel as an id/uuid rather than as free text?

    Jaga wants `referenceValue: true` on every such cell. Dictionary-backed attributes say so
    with a `dictionaryId`; assignees and labels do not, yet their values are still ids.
    """
    return attr.dictionary_id is not None or attr.object_type_name_m in SOURCED_OBJECT_TYPES


def is_renderable(attr: Attribute) -> bool:
    """Can this attribute be shown as a field the user can meaningfully fill in?

    See the module docstring: everything whose values we cannot source is dropped rather than
    rendered as a text box over an id column.
    """
    if attr.object_type_name_m in (TITLE_OBJECT_TYPE, DESCRIPTION_OBJECT_TYPE):
        return True
    return is_reference(attr)


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

    if is_reference(attr):
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


def _choices_for(
    attr: Attribute,
    choices_by_dictionary: dict[int, list[tuple[str, str]]],
    user_choices: list[tuple[str, str]] | None,
    label_choices: list[tuple[str, str]] | None,
) -> list[tuple[str, str]] | None:
    if attr.dictionary_id is not None:
        return choices_by_dictionary.get(attr.dictionary_id, [])
    if attr.object_type_name_m == ASSIGNEE_OBJECT_TYPE:
        return user_choices or []
    if attr.object_type_name_m == LABEL_OBJECT_TYPE:
        return label_choices or []
    return None


def build_attribute_fields(
    attributes: list[Attribute],
    choices_by_dictionary: dict[int, list[tuple[str, str]]],
    title: str,
    description: str,
    user_choices: list[tuple[str, str]] | None = None,
    label_choices: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Build the form fields for the attributes of a task type we can render.

    "Title" and "description" are pre-filled with Sentry data. The space and the task type are
    left out — the cascade selects already carry them (see `injected_attributes`) — and so is
    every attribute whose values we cannot source.
    """
    fields: list[dict[str, Any]] = []
    for attr in sorted(attributes, key=lambda a: a.order_num):
        if not attr.visible:
            continue
        object_type = attr.object_type_name_m
        if object_type in INJECTED_OBJECT_TYPES or object_type in SERVER_OBJECT_TYPES:
            continue
        if not is_renderable(attr):
            _warn_unsupported(attr)
            continue

        default: Any = None
        if object_type == TITLE_OBJECT_TYPE:
            default = title
        elif object_type == DESCRIPTION_OBJECT_TYPE:
            default = description
        choices = _choices_for(attr, choices_by_dictionary, user_choices, label_choices)
        fields.append(attribute_to_field(attr, choices=choices, default=default))
    return fields


def _warn_unsupported(attr: Attribute) -> None:
    """A required attribute we cannot render dooms the create — say so in the logs.

    Jaga will refuse the create with a message of its own ("Поле "..." обязательно для
    заполнения"), which the user does see. What they cannot see is that the plugin knowingly
    left the field out; this log line is the missing half of that story.
    """
    if not attr.required:
        return
    # `attribute_name`, not `name`: `extra` may not shadow a LogRecord field, and `name` is
    # taken by the logger's own name — logging raises KeyError on the clash.
    logger.warning(
        "jaga.fields.required_attribute_not_supported",
        extra={"object_type_name_m": attr.object_type_name_m, "attribute_name": attr.name},
    )


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
            "referenceValue": is_reference(attr),
            "addInfo": {},
        }
        if attr.dictionary_id is not None:
            item["dictionaryId"] = attr.dictionary_id
        payload.append(item)
    return payload


def merge_auto_label(
    payload: list[dict[str, Any]], attributes: list[Attribute], label_id: int
) -> None:
    """Add the automatic label to a create payload — *alongside* the labels the user picked.

    `task.label_id` is a `multiple` attribute, and whoever fills the form may well have chosen
    labels of their own. The automatic label is an addition to that choice, never a replacement,
    so the id is merged into the cell the form produced instead of overwriting it — and a label
    the user happened to pick by hand that IS the automatic one is not sent twice.

    The id travels as a string, exactly as the form's own label values do: `get_labels` offers
    its choices as `(str(id), name)` and Sentry hands back whatever it was given. Injecting a raw
    `int` here would make a hand-picked "17" and an injected 17 two different values in the same
    list — a duplicate Jaga would have to sort out.

    A task type with no labels attribute at all gets nothing: the payload is left alone rather
    than grown a cell whose `fieldId` belongs to no attribute of the type.
    """
    attr = find_attribute(attributes, LABEL_OBJECT_TYPE)
    if attr is None:
        return

    value = str(label_id)
    cell = next((item for item in payload if item["fieldId"] == attr.id), None)
    if cell is None:
        payload.append(
            {
                "fieldId": attr.id,
                "value": [value] if attr.multiple else value,
                "referenceValue": is_reference(attr),
                "addInfo": {},
            }
        )
        return

    if not attr.multiple:
        # A single-valued labels attribute that the user has already filled in: there is no room
        # for both, and their choice is the one that was made on purpose.
        return

    raw = cell["value"]
    chosen = [str(item) for item in (raw if isinstance(raw, list) else [raw])]
    if value not in chosen:
        chosen.append(value)
    cell["value"] = chosen


def injected_attributes(
    attributes: list[Attribute], project_id: int, type_id: int
) -> list[dict[str, Any]]:
    """The space/type cells Jaga demands inside `attributes` on create.

    Both ids are already in the URL of `POST /v1/task/createByTaskType/{project}/{type}`, and
    Jaga still refuses the create without them:

        HTTP 500 — Поле "Пространство" обязательно для заполнения для типа задачи = 33532

    They have no form field of their own (the cascade selects play that role), so nothing in
    `form_data` would ever produce them. Verified against a live instance: with these two cells
    the very same payload is accepted.
    """
    cells: list[dict[str, Any]] = []
    for object_type, value in ((SPACE_OBJECT_TYPE, project_id), (TYPE_OBJECT_TYPE, type_id)):
        attr = find_attribute(attributes, object_type)
        if attr is None:
            continue
        cells.append({"fieldId": attr.id, "value": value, "referenceValue": True, "addInfo": {}})
    return cells


def extract_title(raw_task: dict[str, Any]) -> str:
    """Pull the task title out of a Jaga response; fall back to the task code."""
    for raw in raw_task.get("attributes", []):
        if raw.get("objectTypeNameM") == TITLE_OBJECT_TYPE:
            value = raw.get("value")
            if isinstance(value, str) and value:
                return value
    code = raw_task.get("code", "")
    return str(code)
