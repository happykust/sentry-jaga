import logging
import re
from typing import Any

import pytest

from sentry_jaga.client.exceptions import JagaError
from sentry_jaga.client.models import Attribute, Person, Project, Status, TaskRef, TaskType
from sentry_jaga.fields import (
    ASSIGNEE_OBJECT_TYPE,
    CREATE_TS_OBJECT_TYPE,
    CREATOR_OBJECT_TYPE,
    DESCRIPTION_OBJECT_TYPE,
    LABEL_OBJECT_TYPE,
    SPACE_OBJECT_TYPE,
    TITLE_OBJECT_TYPE,
    TYPE_OBJECT_TYPE,
)
from sentry_jaga.issue_config import (
    CATEGORY_DONE,
    CATEGORY_IN_PROGRESS,
    CATEGORY_TODO,
    EVENT_ATTACHMENT_CONTENT_TYPE,
    GROUP_ID_FIELD,
    MIN_QUERY_LENGTH,
    PERSISTED_FIELDS,
    NoProjectsError,
    apply_assignee_sync,
    apply_status_sync,
    assignee_field_id,
    attach_event_json,
    build_create_config,
    build_link_config,
    create_task_from_form,
    edit_task_comment,
    extract_space_id,
    get_task_summary,
    post_task_comment,
    reachable_status_ids,
    resolve_target_status,
    search_task_summaries,
    status_comment,
)

# The attributes of a real Jaga task type ("Стандарт"), including the two Jaga demands inside
# `attributes` even though they are already in the create URL.
SPACE = Attribute(id=90, name="Space", object_type_name_m=SPACE_OBJECT_TYPE, required=True)
TYPE = Attribute(id=91, name="Task type", object_type_name_m=TYPE_OBJECT_TYPE, required=True)
TITLE = Attribute(id=100, name="Title", object_type_name_m=TITLE_OBJECT_TYPE, required=True)
DESCRIPTION = Attribute(id=101, name="Description", object_type_name_m=DESCRIPTION_OBJECT_TYPE)
ASSIGNEE = Attribute(
    id=103, name="Assignees", object_type_name_m=ASSIGNEE_OBJECT_TYPE, multiple=True
)
LABEL = Attribute(id=104, name="Label", object_type_name_m=LABEL_OBJECT_TYPE, multiple=True)
# A reference with no dictionary behind it: unsupported, dropped from the form.
PRIORITY = Attribute(id=102, name="Priority", object_type_name_m="task.priority_id")
CREATOR = Attribute(id=92, name="Author", object_type_name_m=CREATOR_OBJECT_TYPE)
CREATE_TS = Attribute(id=93, name="Created at", object_type_name_m=CREATE_TS_OBJECT_TYPE)
# A dictionary-backed attribute: the only reference kind that lists itself.
SEVERITY = Attribute(
    id=110,
    name="Severity",
    object_type_name_m="task.flex_severity",
    dictionary_id=55,
    required=True,
)

REAL_ATTRIBUTES = [
    SPACE,
    TYPE,
    TITLE,
    DESCRIPTION,
    ASSIGNEE,
    LABEL,
    PRIORITY,
    CREATOR,
    CREATE_TS,
    SEVERITY,
]

# The assignee select carries EMAILS, not the person UUIDs the attribute stores: a UUID costs one
# HTTP call per person, so it is resolved at submit time for whoever was picked. `PEOPLE` is the
# Jaga directory the fake resolves them against.
USERS = [("ivanov@example.com", "Ivanov Ivan"), ("petrov@example.com", "Petrov Petr")]
PEOPLE = {
    "ivanov@example.com": Person(
        uuid="uuid-1", core_id=193688, team_id=365474, email="ivanov@example.com", name="Ivanov"
    ),
    "petrov@example.com": Person(
        uuid="uuid-2", core_id=193689, team_id=365475, email="petrov@example.com", name="Petrov"
    ),
}
LABELS = [("7", "backend"), ("8", "frontend")]
# The id a live Jaga answered `POST /v1/labels/list {"names": ["sentry"]}` with.
AUTO_LABEL_ID = 17834

# The statuses of one space, as `workflowStatusesAvail` returns them, Jaga's own names kept
# verbatim. RUF001 reads the one-letter Cyrillic preposition below as a Latin "B".
IN_PROGRESS_NAME = "В работе"  # noqa: RUF001
TODO_STATUS = Status(id=107391, name="Сделать", category=CATEGORY_TODO)
IN_PROGRESS_STATUS = Status(id=107389, name=IN_PROGRESS_NAME, category=CATEGORY_IN_PROGRESS)
DONE_STATUS = Status(id=107390, name="Готово", category=CATEGORY_DONE)
SPACE_STATUSES = [TODO_STATUS, IN_PROGRESS_STATUS, DONE_STATUS]

SPACE_ID = 11361
TASK_ID = 500


def raw_task(
    *,
    space_id: Any = SPACE_ID,
    transitions: list[Any] | None = None,
    with_space: bool = True,
    with_assignee: bool = True,
) -> dict[str, Any]:
    """A task as `findExtendedWithFlexField` returns it: the space is an ordinary attribute, and
    the statuses it can move to are listed on the task itself."""
    attributes: list[dict[str, Any]] = [
        {"fieldId": 100, "value": "Login is broken", "objectTypeNameM": TITLE_OBJECT_TYPE}
    ]
    if with_assignee:
        # A cell carries its own `fieldId`, which is what spares the assignee sync a round trip.
        attributes.append({"fieldId": 103, "value": [], "objectTypeNameM": ASSIGNEE_OBJECT_TYPE})
    if with_space:
        attributes.append({"fieldId": 90, "value": space_id, "objectTypeNameM": SPACE_OBJECT_TYPE})
    return {
        "id": TASK_ID,
        "code": "PLT-500",
        "status": {"id": TODO_STATUS.id, "name": TODO_STATUS.name},
        "statusTransitions": [IN_PROGRESS_STATUS.id, DONE_STATUS.id]
        if transitions is None
        else transitions,
        "attributes": attributes,
    }


class FakeClient:
    """A stand-in JagaClient: same methods, records the calls."""

    def __init__(
        self,
        projects: list[Project] | None = None,
        task_types: list[TaskType] | None = None,
        attributes: list[Attribute] | None = None,
        task_types_by_project: dict[int, list[TaskType]] | None = None,
        task: dict[str, Any] | None = None,
        statuses: list[Status] | None = None,
    ) -> None:
        self._projects = projects if projects is not None else [Project(1, "Platform", "PLT")]
        self._task_types = task_types if task_types is not None else [TaskType(10, "Bug")]
        self._task_types_by_project = task_types_by_project
        self._attributes = attributes if attributes is not None else list(REAL_ATTRIBUTES)
        self._task = task
        self._statuses = SPACE_STATUSES if statuses is None else statuses
        self.created: dict[str, Any] | None = None
        self.comments: list[tuple[int, str]] = []
        self.updated_comments: list[tuple[int, int, str]] = []
        self.searches: list[str] = []
        self.attributes_requested: list[tuple[int, int]] = []
        self.users_requested: list[int] = []
        self.labels_requested = 0
        self.transitions: list[tuple[int, int]] = []
        self.statuses_requested: list[int] = []
        self.tasks_fetched: list[str] = []
        self.labels_resolved: list[str] = []
        self.attachments: list[tuple[int, int, str, bytes, str]] = []
        self.people_resolved: list[str] = []
        self.assignees_set: list[tuple[int, int, list[str]]] = []

    def get_projects(self) -> list[Project]:
        return self._projects

    def get_task_types(self, project_id: int) -> list[TaskType]:
        if self._task_types_by_project is not None:
            return self._task_types_by_project.get(project_id, [])
        return self._task_types

    def get_task_type_attributes(self, project_id: int, task_type_id: int) -> list[Attribute]:
        self.attributes_requested.append((project_id, task_type_id))
        return self._attributes

    def get_dictionary_values(self, dictionary_id: int) -> list[tuple[str, str]]:
        return [("1", "High"), ("2", "Low")]

    def get_space_users(self, space_id: int) -> list[tuple[str, str]]:
        self.users_requested.append(space_id)
        return USERS

    def find_person_by_email(self, email: str) -> Person | None:
        self.people_resolved.append(email)
        return PEOPLE.get(email)

    def set_task_assignees(self, task_id: int, field_id: int, person_uuids: list[str]) -> None:
        self.assignees_set.append((task_id, field_id, person_uuids))

    def get_labels(self) -> list[tuple[str, str]]:
        self.labels_requested += 1
        return LABELS

    def get_or_create_label(self, name: str) -> int:
        self.labels_resolved.append(name)
        return AUTO_LABEL_ID

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
        self.tasks_fetched.append(code)
        task = dict(self._task) if self._task is not None else raw_task()
        task["code"] = code
        return task

    def search_tasks_globally(self, text: str, *, size: int = 20) -> list[TaskRef]:
        self.searches.append(text)
        return [TaskRef(id=5, code="PLT-5", title="Login is broken")]

    def create_comment(self, task_id: int, content: str) -> dict[str, Any]:
        self.comments.append((task_id, content))
        return {"id": 900 + len(self.comments), "taskId": task_id, "contentComment": content}

    def update_comment(self, comment_id: int, task_id: int, content: str) -> dict[str, Any]:
        self.updated_comments.append((comment_id, task_id, content))
        return {"id": comment_id, "taskId": task_id, "contentComment": content}

    def get_space_statuses(self, space_id: int) -> list[Status]:
        self.statuses_requested.append(space_id)
        return self._statuses

    def transition_task(self, task_id: int, target_status_id: int) -> None:
        self.transitions.append((task_id, target_status_id))

    def attach_file(
        self, space_id: int, task_id: int, filename: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        self.attachments.append((space_id, task_id, filename, content, content_type))
        return {"id": 1901762, "attachName": filename}


def _by_name(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in fields}


def _cell(payload: list[dict[str, Any]], field_id: int) -> dict[str, Any] | None:
    return next((item for item in payload if item["fieldId"] == field_id), None)


def test_create_config_builds_cascade() -> None:
    fields = build_create_config(FakeClient(), {}, "Login is broken", "Sentry issue: http://s/1")
    by_name = _by_name(fields)

    assert by_name["project"]["updatesForm"] is True
    assert by_name["project"]["choices"] == [("1", "Platform (PLT)")]
    assert by_name["issue_type"]["updatesForm"] is True
    assert by_name["issue_type"]["choices"] == [("10", "Bug")]
    assert by_name["title"]["default"] == "Login is broken"
    assert by_name["description"]["default"] == "Sentry issue: http://s/1"
    assert by_name["attr_110"]["choices"] == [("1", "High"), ("2", "Low")]


def test_create_config_hides_the_attributes_the_cascade_already_asks_for(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cascade selects already carry the space and the type, and the author and creation date
    are Jaga's to fill: they are dropped ON PURPOSE, so no "unsupported required attribute" warning
    may blame two fields the plugin does in fact submit."""
    with caplog.at_level(logging.WARNING, logger="sentry_jaga.fields"):
        fields = build_create_config(FakeClient(), {}, "t", "d")

    names = [f["name"] for f in fields]
    assert "attr_90" not in names  # task.project_id
    assert "attr_91" not in names  # task.type_id
    assert "attr_92" not in names  # task.creator_id
    assert "attr_93" not in names  # task.create_ts
    # ...and exactly one field asks for the space.
    assert [f["label"] for f in fields].count("Space") == 1
    assert "required_attribute_not_supported" not in caplog.text


def test_create_config_renders_assignees_from_space_members() -> None:
    fields = build_create_config(FakeClient(), {}, "t", "d")
    assignee = _by_name(fields)["attr_103"]

    assert assignee["type"] == "select"
    assert assignee["multiple"] is True
    assert assignee["choices"] == USERS


def test_create_config_renders_labels() -> None:
    fields = build_create_config(FakeClient(), {}, "t", "d")
    label = _by_name(fields)["attr_104"]

    assert label["type"] == "select"
    assert label["multiple"] is True
    assert label["choices"] == LABELS


def test_create_config_skips_an_unsupported_reference_attribute() -> None:
    """Priority is a reference with no dictionary: its values cannot be listed, so it is left out
    rather than rendered as a text box the user would fill with "High"."""
    fields = build_create_config(FakeClient(), {}, "t", "d")
    assert "attr_102" not in _by_name(fields)


def test_create_config_warns_about_a_required_attribute_it_cannot_render(
    caplog: pytest.LogCaptureFixture,
) -> None:
    required_priority = Attribute(
        id=102, name="Priority", object_type_name_m="task.priority_id", required=True
    )
    client = FakeClient(attributes=[TITLE, required_priority])

    with caplog.at_level(logging.WARNING, logger="sentry_jaga.fields"):
        fields = build_create_config(client, {}, "t", "d")

    assert "attr_102" not in _by_name(fields)
    assert "required_attribute_not_supported" in caplog.text


def test_create_config_does_not_fetch_users_or_labels_when_unused() -> None:
    """The create form re-renders on every change of the cascade, so a task type without assignees
    or labels must not pay an HTTP request per render."""
    client = FakeClient(attributes=[TITLE, DESCRIPTION])

    build_create_config(client, {}, "t", "d")

    assert client.users_requested == []
    assert client.labels_requested == 0


def test_create_config_fetches_space_members_for_the_selected_space() -> None:
    client = FakeClient(
        projects=[Project(1, "Platform", "PLT"), Project(2, "Billing", "BIL")],
    )
    build_create_config(client, {"project": "2"}, "t", "d")

    assert client.users_requested == [2]


def test_create_config_honours_selected_params() -> None:
    client = FakeClient(
        projects=[Project(1, "Platform", "PLT"), Project(2, "Billing", "BIL")],
        task_types=[TaskType(10, "Bug"), TaskType(11, "Task")],
    )
    fields = build_create_config(client, {"project": "2", "issue_type": "11"}, "t", "d")
    by_name = _by_name(fields)

    assert by_name["project"]["default"] == "2"
    assert by_name["issue_type"]["default"] == "11"


def test_create_config_drops_task_type_left_over_from_previous_project() -> None:
    """On `updatesForm` Sentry resends EVERY field, so after a space switch `params` still carries
    the OLD space's `issue_type` — taken at face value, that files a task of a foreign type."""
    client = FakeClient(
        projects=[Project(1, "Platform", "PLT"), Project(2, "Billing", "BIL")],
        task_types_by_project={
            1: [TaskType(10, "Bug"), TaskType(11, "Task")],
            2: [TaskType(20, "Incident"), TaskType(21, "Request")],
        },
    )

    fields = build_create_config(client, {"project": "2", "issue_type": "10"}, "t", "d")
    by_name = _by_name(fields)

    assert by_name["issue_type"]["default"] == "20"
    assert by_name["issue_type"]["choices"] == [("20", "Incident"), ("21", "Request")]
    assert client.attributes_requested == [(2, 20)]


def test_create_config_falls_back_when_project_is_unknown() -> None:
    """A space from params that is no longer in the list must not leak any further."""
    client = FakeClient(projects=[Project(1, "Platform", "PLT"), Project(2, "Billing", "BIL")])
    by_name = _by_name(build_create_config(client, {"project": "999"}, "t", "d"))

    assert by_name["project"]["default"] == "1"
    assert client.attributes_requested == [(1, 10)]


@pytest.mark.parametrize(
    "junk", ["", "  ", "not-a-number", None], ids=["empty", "blank", "text", "none"]
)
def test_create_config_ignores_unusable_project_param(junk: str | None) -> None:
    """An empty or non-numeric value from the form must not break the cascade."""
    client = FakeClient(projects=[Project(1, "Platform", "PLT"), Project(2, "Billing", "BIL")])
    by_name = _by_name(build_create_config(client, {"project": junk}, "t", "d"))

    assert by_name["project"]["default"] == "1"


def test_create_config_without_projects_raises() -> None:
    with pytest.raises(NoProjectsError):
        build_create_config(FakeClient(projects=[]), {}, "t", "d")


def test_create_config_without_task_types_stops_at_project() -> None:
    fields = build_create_config(FakeClient(task_types=[]), {}, "t", "d")
    assert [f["name"] for f in fields] == ["project"]


# --- create ----------------------------------------------------------------


def test_create_task_from_form_injects_space_and_type() -> None:
    """REGRESSION: Jaga answers 500 to a create whose `attributes` omit `task.project_id` and
    `task.type_id`, even though both ids are already in the create URL — and neither has a form
    field of its own, so a payload built from `attr_*` keys alone can never contain them."""
    client = FakeClient()

    create_task_from_form(
        client,
        {"project": "1", "issue_type": "10", "title": "Login is broken"},
    )

    assert client.created is not None
    payload = client.created["attributes"]

    assert _cell(payload, SPACE.id) == {
        "fieldId": 90,
        "value": 1,
        "referenceValue": True,
        "addInfo": {},
    }
    assert _cell(payload, TYPE.id) == {
        "fieldId": 91,
        "value": 10,
        "referenceValue": True,
        "addInfo": {},
    }


def test_create_task_from_form_accepts_the_payload_an_alert_rule_sends() -> None:
    """A ticket rule hands `create_issue` its saved config with `title`/`description` overwritten
    from the event that fired: if the title misses the Jaga title cell, every task a rule files is
    nameless."""
    client = FakeClient()

    result = create_task_from_form(
        client,
        {
            "project": "1",
            "issue_type": "10",
            # Written by the rule from the event that fired, not by the admin.
            "title": "ZeroDivisionError: division by zero",
            "description": "Sentry issue: http://s/1\n\nThis task was created automatically",
        },
    )

    assert client.created is not None
    payload = client.created["attributes"]

    title_cell = _cell(payload, TITLE.id)
    assert title_cell is not None, "the event title never reached Jaga's title attribute"
    assert title_cell["value"] == "ZeroDivisionError: division by zero"

    description_cell = _cell(payload, DESCRIPTION.id)
    assert description_cell is not None
    assert "created automatically" in description_cell["value"]

    assert result["title"] == "ZeroDivisionError: division by zero"


def test_create_task_from_form_sends_attributes() -> None:
    client = FakeClient()
    result = create_task_from_form(
        client,
        {
            "project": "1",
            "issue_type": "10",
            "title": "Login is broken",
            "description": "body",
            "attr_110": "1",
        },
    )

    assert result["key"] == "PLT-500"
    assert result["title"] == "Login is broken"
    assert client.created is not None
    assert client.created["project_id"] == 1
    assert client.created["task_type_id"] == 10

    payload = client.created["attributes"]
    assert _cell(payload, 100) is not None
    assert _cell(payload, 101) is not None
    assert _cell(payload, 110) is not None


def test_create_task_from_form_swaps_the_chosen_emails_for_uuids() -> None:
    """The select submits emails and Jaga stores UUIDs: only the people actually picked are looked
    up, one call each at submit, instead of every member of the space at render time."""
    client = FakeClient()
    create_task_from_form(
        client,
        {
            "project": "1",
            "issue_type": "10",
            "title": "Login is broken",
            "attr_103": ["ivanov@example.com", "petrov@example.com"],
            "attr_104": ["7"],
        },
    )

    assert client.created is not None
    payload = client.created["attributes"]

    assignee = _cell(payload, 103)
    assert assignee is not None
    assert assignee["value"] == ["uuid-1", "uuid-2"]
    assert assignee["referenceValue"] is True
    assert client.people_resolved == ["ivanov@example.com", "petrov@example.com"]

    label = _cell(payload, 104)
    assert label is not None
    assert label["value"] == ["7"]
    assert label["referenceValue"] is True


def test_create_task_from_form_refuses_an_assignee_jaga_does_not_know() -> None:
    """Filing the task without the assignee the user picked would be a silent lie, and sending an
    email where a UUID belongs makes Jaga refuse the create: name who could not be found."""
    client = FakeClient()

    with pytest.raises(JagaError, match=re.escape("ghost@example.com")):
        create_task_from_form(
            client,
            {
                "project": "1",
                "issue_type": "10",
                "title": "Login is broken",
                "attr_103": ["ghost@example.com"],
            },
        )

    assert client.created is None, "nothing may be filed when the assignee cannot be resolved"


def test_create_task_from_form_costs_no_lookup_when_nobody_was_picked() -> None:
    """The common case: a task filed with no assignee must not talk to the user directory."""
    client = FakeClient()
    create_task_from_form(client, {"project": "1", "issue_type": "10", "title": "Login is broken"})

    assert client.people_resolved == []


def test_create_task_from_form_rejects_empty_form() -> None:
    """An empty form is refused before the injected cells make the payload look non-empty."""
    with pytest.raises(JagaError):
        create_task_from_form(FakeClient(), {"project": "1", "issue_type": "10"})


# --- carrying the Sentry issue through the form, and the event onto the task ---------------


def test_the_create_form_carries_the_sentry_issue_in_a_hidden_field() -> None:
    """`create_issue` is handed the form data and nothing else — no group, no event — so a hidden
    field, whose default the frontend submits, is the only way the issue can reach it."""
    fields = build_create_config(FakeClient(), {}, "t", "d", group_id="4242")
    by_name = _by_name(fields)

    assert by_name[GROUP_ID_FIELD]["type"] == "hidden"
    assert by_name[GROUP_ID_FIELD]["default"] == "4242"


def test_the_form_of_an_alert_rule_carries_no_issue() -> None:
    """The ticket-rule modal renders this form with no group and SAVES what it gets: a group id in
    there would be frozen into the rule, attaching one long-dead event to every task it files."""
    fields = build_create_config(FakeClient(), {}, "t", "d")

    assert GROUP_ID_FIELD not in _by_name(fields)


def test_the_hidden_field_survives_a_task_type_less_space() -> None:
    """The form stops at the space select when the space has no task types, and must still carry
    the issue — otherwise picking a space silently throws it away."""
    client = FakeClient(task_types=[])
    fields = build_create_config(client, {}, "t", "d", group_id="4242")

    assert _by_name(fields)[GROUP_ID_FIELD]["default"] == "4242"


def test_attach_event_json_names_the_file_after_the_event() -> None:
    """A task can be filed from the same issue twice, and two files called `sentry-event.json` on
    one task tell nobody which event is which."""
    client = FakeClient()
    attach_event_json(
        client,
        space_id=11361,
        task_id=1703944,
        event_id="deadbeef",
        content=b'{"event_id": "deadbeef"}',
    )

    assert client.attachments == [
        (
            11361,
            1703944,
            "sentry-event-deadbeef.json",
            b'{"event_id": "deadbeef"}',
            EVENT_ATTACHMENT_CONTENT_TYPE,
        )
    ]


def test_attach_event_json_falls_back_to_a_plain_name() -> None:
    client = FakeClient()
    attach_event_json(client, space_id=1, task_id=2, event_id="", content=b"{}")

    assert client.attachments[0][2] == "sentry-event.json"


# --- the label every task from Sentry carries ---------------------------------------------


def test_the_auto_label_is_put_on_the_task() -> None:
    """Every task filed from Sentry is tagged so that one Jaga filter finds them all; the name is
    resolved through a get-or-create, since a fresh Jaga does not have the label yet."""
    client = FakeClient()
    create_task_from_form(
        client,
        {"project": "1", "issue_type": "10", "title": "Login is broken"},
        auto_label="sentry",
    )

    assert client.labels_resolved == ["sentry"]
    assert client.created is not None
    assert _cell(client.created["attributes"], 104) == {
        "fieldId": 104,
        "value": [str(AUTO_LABEL_ID)],
        "referenceValue": True,
        "addInfo": {},
    }


def test_the_auto_label_joins_the_labels_the_user_picked() -> None:
    """`task.label_id` is `multiple` and the form offers it, so the automatic label joins whatever
    the user picked instead of overwriting it."""
    client = FakeClient()
    create_task_from_form(
        client,
        {"project": "1", "issue_type": "10", "title": "t", "attr_104": ["7", "8"]},
        auto_label="sentry",
    )

    assert client.created is not None
    label = _cell(client.created["attributes"], 104)
    assert label is not None
    assert label["value"] == ["7", "8", str(AUTO_LABEL_ID)]


def test_an_empty_auto_label_is_the_off_switch() -> None:
    """The organization cleared the setting: no label, and no HTTP call to resolve one."""
    client = FakeClient()
    create_task_from_form(client, {"project": "1", "issue_type": "10", "title": "t"}, auto_label="")

    assert client.labels_resolved == []
    assert client.created is not None
    assert _cell(client.created["attributes"], 104) is None


def test_a_blank_auto_label_is_not_a_label() -> None:
    """A box with a space in it is an empty box, not a label named " "."""
    client = FakeClient()
    create_task_from_form(
        client, {"project": "1", "issue_type": "10", "title": "t"}, auto_label="   "
    )

    assert client.labels_resolved == []


def test_a_task_type_without_labels_is_filed_without_one() -> None:
    """Not every task type has a label attribute, and such a type must still be filable — without
    a get-or-create whose id could only go into a cell Jaga would reject."""
    client = FakeClient(attributes=[SPACE, TYPE, TITLE, DESCRIPTION])
    create_task_from_form(
        client,
        {"project": "1", "issue_type": "10", "title": "Login is broken"},
        auto_label="sentry",
    )

    assert client.labels_resolved == []
    assert client.created is not None
    assert _cell(client.created["attributes"], 104) is None
    assert _cell(client.created["attributes"], 100) is not None


def test_a_task_created_without_an_auto_label_argument_carries_none() -> None:
    """The core defaults to no label: the name comes from the organization config, which only the
    Sentry layer can read."""
    client = FakeClient()
    create_task_from_form(client, {"project": "1", "issue_type": "10", "title": "t"})

    assert client.labels_resolved == []
    assert client.created is not None
    assert _cell(client.created["attributes"], 104) is None


def test_create_task_from_form_returns_task_id_in_metadata() -> None:
    """`metadata` travels into `ExternalIssue`: without `task_id` every resolve would look the task
    up by code again, though Jaga has just returned the id."""
    result = create_task_from_form(
        FakeClient(), {"project": "1", "issue_type": "10", "title": "Login is broken"}
    )

    assert result["metadata"] == {"task_id": 500}


def test_warns_when_no_system_attribute_recognised(caplog: pytest.LogCaptureFixture) -> None:
    """The title/description mnemonics are not a documented contract, and a form that recognised
    none of them would come out with no title and no Sentry context — silently."""
    exotic = Attribute(id=200, name="Subject", object_type_name_m="task.subject_line")

    with caplog.at_level(logging.WARNING, logger="sentry_jaga.issue_config"):
        build_create_config(FakeClient(attributes=[exotic]), {}, "t", "d")

    assert "system_attributes_not_found" in caplog.text


def test_does_not_warn_when_system_attributes_are_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="sentry_jaga.issue_config"):
        build_create_config(FakeClient(attributes=[TITLE, DESCRIPTION]), {}, "t", "d")

    assert caplog.records == []


# --- link, search, sync ----------------------------------------------------


def test_link_config_searches_every_space_at_once() -> None:
    """No space to pick first: Jaga's per-space search demands a `projectId`, the global one does
    not."""
    client = FakeClient()
    fields = build_link_config(client, {"query": "login"})
    by_name = _by_name(fields)

    assert client.searches == ["login"]
    assert by_name["query"]["updatesForm"] is True
    assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]


def test_link_config_no_longer_asks_for_a_space() -> None:
    """The space select is gone from BOTH shapes of the form, and with it the only reason the link
    form had to ask Jaga for the list of spaces."""
    client = FakeClient(projects=[])

    fallback = _by_name(build_link_config(client, {"query": "login"}))
    live = _by_name(build_link_config(client, {}, search_url="/extensions/jaga/search/o/1/"))

    assert "project" not in fallback
    assert "project" not in live
    # A service account with no spaces at all can still link a task it can see.
    assert fallback["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]


def test_link_config_shows_the_code_and_the_title_only() -> None:
    """The global search returns a task's space as null, and reading it back would cost one fetch
    per suggestion per keystroke — so a suggestion is `code — title`."""
    by_name = _by_name(build_link_config(FakeClient(), {"query": "login"}))

    assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]


def test_link_config_without_query_has_no_choices() -> None:
    by_name = _by_name(build_link_config(FakeClient(), {}))
    assert by_name["externalIssue"]["choices"] == []


def test_link_config_does_not_search_below_min_query_length() -> None:
    """`updatesForm` re-fetches the config on EVERY keystroke with no debounce, so the minimum
    query length is the only brake there is."""
    client = FakeClient()
    short = "l" * (MIN_QUERY_LENGTH - 1)

    by_name = _by_name(build_link_config(client, {"query": short}))

    assert client.searches == []
    assert by_name["externalIssue"]["choices"] == []


def test_link_config_searches_from_min_query_length() -> None:
    client = FakeClient()
    enough = "l" * MIN_QUERY_LENGTH

    by_name = _by_name(build_link_config(client, {"query": enough}))

    assert client.searches == [enough]
    assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]


def test_link_config_ignores_whitespace_only_query() -> None:
    client = FakeClient()
    build_link_config(client, {"query": "   "})
    assert client.searches == []


def test_link_config_help_states_the_minimum_query_length() -> None:
    by_name = _by_name(build_link_config(FakeClient(), {}))
    assert str(MIN_QUERY_LENGTH) in by_name["query"]["help"]


# --- the two shapes of the link form: with and without the search endpoint ---------------


def test_link_config_with_a_search_url_builds_an_async_select() -> None:
    """`url` on a select is what makes Sentry's frontend render it as a debounced autocomplete;
    the `query` box only stands in for that, so it must be gone rather than doubled."""
    client = FakeClient()

    fields = build_link_config(client, {}, search_url="/extensions/jaga/search/o/1/")
    by_name = _by_name(fields)

    external_issue = by_name["externalIssue"]
    assert external_issue["url"] == "/extensions/jaga/search/o/1/"
    assert external_issue["type"] == "select"
    assert "query" not in by_name

    # Rendering the form now costs NOTHING: no search, and not even the list of spaces.
    assert client.searches == []


def test_link_config_without_a_search_url_keeps_the_updates_form_search() -> None:
    """The endpoint is only mounted if the admin set `ROOT_URLCONF`; without it the form must fall
    back to `query` + `updatesForm`, not render a dead select."""
    by_name = _by_name(build_link_config(FakeClient(), {"query": "login"}))

    assert "url" not in by_name["externalIssue"]
    assert by_name["query"]["updatesForm"] is True
    assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]


def test_get_task_summary() -> None:
    summary = get_task_summary(FakeClient(), "PLT-500")
    assert summary["key"] == "PLT-500"
    assert summary["title"] == "Login is broken"
    assert summary["metadata"] == {"task_id": 500}


def test_search_task_summaries() -> None:
    assert search_task_summaries(FakeClient(), "login") == [
        {"key": "PLT-5", "title": "Login is broken"}
    ]


def test_search_task_summaries_without_query_is_empty() -> None:
    assert search_task_summaries(FakeClient(), "") == []


def test_status_comment_distinguishes_resolution() -> None:
    assert "resolved" in status_comment(is_resolved=True).lower()
    assert "reopened" in status_comment(is_resolved=False).lower()


# --- the space a task lives in --------------------------------------------------------


def test_extract_space_id() -> None:
    assert extract_space_id(raw_task()) == SPACE_ID


def test_extract_space_id_when_the_value_arrives_as_a_list() -> None:
    """Jaga wraps multi-valued attributes in a list, and a list must not turn into
    `int(['11361'])`."""
    assert extract_space_id(raw_task(space_id=[SPACE_ID])) == SPACE_ID


def test_extract_space_id_from_a_string_value() -> None:
    assert extract_space_id(raw_task(space_id="11361")) == SPACE_ID


def test_extract_space_id_without_the_attribute_is_none() -> None:
    assert extract_space_id(raw_task(with_space=False)) is None


@pytest.mark.parametrize("value", [None, "", [], "not-a-number"])
def test_extract_space_id_of_an_unusable_value_is_none(value: Any) -> None:
    """None rather than a crash: a task we cannot place in a space still gets its comment."""
    assert extract_space_id(raw_task(space_id=value)) is None


# --- which statuses a task can reach --------------------------------------------------


def test_reachable_status_ids_deduplicates_and_keeps_the_order() -> None:
    """A live instance repeats ids in `statusTransitions`, and their order is the one targets are
    preferred in — so it has to survive deduplication."""
    task = raw_task(transitions=[107390, 107389, 107390, 107389, 107391])
    assert reachable_status_ids(task) == [107390, 107389, 107391]


def test_reachable_status_ids_when_there_are_none() -> None:
    assert reachable_status_ids(raw_task(transitions=[])) == []


def test_reachable_status_ids_skips_junk_instead_of_raising() -> None:
    """`sync_status_outbound` only catches `JagaError`, so a ValueError escaping here would take
    the Sentry-side resolve down with it."""
    task = raw_task(transitions=[None, "oops", DONE_STATUS.id])

    assert reachable_status_ids(task) == [DONE_STATUS.id]


def test_resolve_target_status_picks_the_status_of_the_wanted_category() -> None:
    client = FakeClient()
    target = resolve_target_status(client, raw_task(), SPACE_ID, CATEGORY_DONE)

    assert target == DONE_STATUS
    assert client.statuses_requested == [SPACE_ID]


def test_resolve_target_status_ignores_a_status_the_task_cannot_reach() -> None:
    """The done status is in the wanted category, but the workflow does not allow the task there
    from where it stands: moving it anyway is a 4xx from Jaga."""
    task = raw_task(transitions=[IN_PROGRESS_STATUS.id])  # "Готово" is NOT reachable

    assert resolve_target_status(FakeClient(), task, SPACE_ID, CATEGORY_DONE) is None


def test_resolve_target_status_without_any_transitions_is_none() -> None:
    task = raw_task(transitions=[])

    client = FakeClient()
    assert resolve_target_status(client, task, SPACE_ID, CATEGORY_DONE) is None
    # A task that can go nowhere needs no list of statuses to compare against.
    assert client.statuses_requested == []


# --- the sync itself ------------------------------------------------------------------


def _sync(client: FakeClient, **kwargs: Any) -> str:
    defaults: dict[str, Any] = {
        "is_resolved": True,
        "resolved_category": CATEGORY_DONE,
        "unresolved_category": CATEGORY_TODO,
        "post_comment": True,
    }
    return apply_status_sync(client, "PLT-500", **{**defaults, **kwargs})


def test_apply_status_sync_moves_the_task_on_resolve() -> None:
    client = FakeClient()

    result = _sync(client, post_comment=False)

    assert client.transitions == [(TASK_ID, DONE_STATUS.id)]
    assert "Готово" in result


def test_apply_status_sync_moves_the_task_back_on_regression() -> None:
    """A reopen uses the *unresolved* category — the whole point of having two settings."""
    client = FakeClient(task=raw_task(transitions=[TODO_STATUS.id, DONE_STATUS.id]))

    _sync(client, is_resolved=False, post_comment=False)

    assert client.transitions == [(TASK_ID, TODO_STATUS.id)]


def test_apply_status_sync_does_not_comment_when_the_move_worked() -> None:
    """`post_comment=False` means the move IS the notification — no comment on top of it."""
    client = FakeClient()

    result = _sync(client, post_comment=False)

    assert client.transitions == [(TASK_ID, DONE_STATUS.id)]
    assert client.comments == []
    assert "no comment" in result


def test_apply_status_sync_comments_on_top_of_the_move_when_asked() -> None:
    client = FakeClient()

    _sync(client, post_comment=True)

    assert client.transitions == [(TASK_ID, DONE_STATUS.id)]
    assert [task_id for task_id, _ in client.comments] == [TASK_ID]
    assert "resolved" in client.comments[0][1].lower()


def test_apply_status_sync_never_moves_to_an_unreachable_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The workflow offers no way into "done" from here, so the task stays put and the comment is
    the fallback — even with `post_comment=False`."""
    client = FakeClient(task=raw_task(transitions=[IN_PROGRESS_STATUS.id]))

    with caplog.at_level(logging.WARNING, logger="sentry_jaga.issue_config"):
        result = _sync(client, post_comment=False)

    assert client.transitions == []
    assert [task_id for task_id, _ in client.comments] == [TASK_ID]
    assert "not moved" in result

    record = next(r for r in caplog.records if r.message == "jaga.sync.no_status_in_category")
    assert record.category == CATEGORY_DONE  # type: ignore[attr-defined]
    assert record.task_code == "PLT-500"  # type: ignore[attr-defined]
    assert record.reachable_status_ids == [IN_PROGRESS_STATUS.id]  # type: ignore[attr-defined]


def test_apply_status_sync_deduplicates_the_transitions_it_was_given() -> None:
    """Duplicates in `statusTransitions` are real, and must not turn into two moves."""
    client = FakeClient(
        task=raw_task(transitions=[DONE_STATUS.id, DONE_STATUS.id, IN_PROGRESS_STATUS.id])
    )

    _sync(client, post_comment=False)

    assert client.transitions == [(TASK_ID, DONE_STATUS.id)]


def test_apply_status_sync_comments_when_the_task_has_no_space(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without a space there is nothing to resolve status ids against, so comment anyway and log
    it — silence would read as a sync that simply does not work."""
    client = FakeClient(task=raw_task(with_space=False))

    with caplog.at_level(logging.WARNING, logger="sentry_jaga.issue_config"):
        _sync(client)

    assert client.transitions == []
    assert client.statuses_requested == []
    assert [task_id for task_id, _ in client.comments] == [TASK_ID]
    assert any(r.message == "jaga.sync.space_not_found_on_task" for r in caplog.records)


def test_apply_status_sync_honours_a_category_the_admin_chose() -> None:
    """The categories are settings, not constants: an org that moves resolved issues to "In
    progress" for a human to close by hand must get exactly that."""
    client = FakeClient()

    _sync(client, resolved_category=CATEGORY_IN_PROGRESS, post_comment=False)

    assert client.transitions == [(TASK_ID, IN_PROGRESS_STATUS.id)]


# --- remembering the last space and task type (`defaults`) ---------------------------------
#
# A remembered value is a *starting* point, never an authority: between the last create and this
# render the space may have been archived, or the service account may have lost access to it.


def _two_spaces() -> FakeClient:
    return FakeClient(
        projects=[Project(1, "Platform", "PLT"), Project(2, "Billing", "BIL")],
        task_types_by_project={
            1: [TaskType(10, "Bug"), TaskType(11, "Task")],
            2: [TaskType(20, "Incident"), TaskType(21, "Request")],
        },
    )


def test_create_config_starts_from_the_remembered_space_and_type() -> None:
    """With no params (a freshly opened form), the remembered pair is what renders."""
    fields = build_create_config(
        _two_spaces(), {}, "t", "d", defaults={"project": "2", "issue_type": "21"}
    )
    by_name = _by_name(fields)

    assert by_name["project"]["default"] == "2"
    assert by_name["issue_type"]["default"] == "21"


def test_the_live_form_beats_the_remembered_choice() -> None:
    """The user is switching the space right now: a remembered choice that won would snap the
    select back under their fingers."""
    fields = build_create_config(
        _two_spaces(), {"project": "1"}, "t", "d", defaults={"project": "2", "issue_type": "21"}
    )
    by_name = _by_name(fields)

    assert by_name["project"]["default"] == "1"
    # The remembered type belonged to space 2 and cannot survive the switch either.
    assert by_name["issue_type"]["default"] == "10"


def test_a_blank_param_does_not_wipe_the_remembered_choice() -> None:
    """An unset select serialises to "", which is the absence of a choice, not a choice: letting it
    through would put the form back on the first space and undo the whole feature."""
    fields = build_create_config(
        _two_spaces(),
        {"project": "", "issue_type": ""},
        "t",
        "d",
        defaults={"project": "2", "issue_type": "21"},
    )
    by_name = _by_name(fields)

    assert by_name["project"]["default"] == "2"
    assert by_name["issue_type"]["default"] == "21"


def test_a_remembered_space_that_is_gone_falls_back_instead_of_breaking() -> None:
    """The service account has lost access to the remembered space, so Jaga no longer lists it: the
    form must open on a space that exists rather than raise or offer one nobody can submit to."""
    client = _two_spaces()

    fields = build_create_config(
        client, {}, "t", "d", defaults={"project": "999", "issue_type": "888"}
    )
    by_name = _by_name(fields)

    assert by_name["project"]["default"] == "1"
    assert by_name["issue_type"]["default"] == "10"
    # And the attributes were fetched for the space that actually rendered, not the ghost one.
    assert client.attributes_requested == [(1, 10)]


def test_persisted_field_names_are_the_names_the_form_actually_emits() -> None:
    """Sentry filters the submitted form by `PERSISTED_FIELDS`, so a field renamed in
    `build_create_config` alone would silently stop the form remembering anything."""
    names = {field["name"] for field in build_create_config(FakeClient(), {}, "t", "d")}

    assert set(PERSISTED_FIELDS) <= names


# --- the comment posted when an existing task is linked ------------------------------------


def test_link_config_prefills_a_comment_pointing_back_at_sentry() -> None:
    fields = build_link_config(
        FakeClient(), {}, search_url="/search", sentry_url="https://sentry.io/issues/1/"
    )
    by_name = _by_name(fields)

    assert by_name["comment"]["default"] == "Linked to Sentry issue https://sentry.io/issues/1/"
    # It must be editable and optional — clearing it is how a user declines the comment.
    assert by_name["comment"]["required"] is False
    assert by_name["comment"]["type"] == "textarea"


def test_link_config_prefills_the_comment_on_the_fallback_search_too() -> None:
    """The comment is offered in both shapes of the link form: it is not a property of the
    search."""
    fields = build_link_config(FakeClient(), {}, sentry_url="https://sentry.io/issues/1/")
    by_name = _by_name(fields)

    assert by_name["comment"]["default"] == "Linked to Sentry issue https://sentry.io/issues/1/"


def test_link_config_offers_no_comment_without_a_sentry_url() -> None:
    """No URL, no comment: a field pre-filled with "Linked to Sentry issue None" is worse than no
    field at all."""
    assert "comment" not in _by_name(build_link_config(FakeClient(), {}))


# --- posting and editing comments ----------------------------------------------------------


def test_post_task_comment_resolves_the_code_and_hands_back_the_created_comment() -> None:
    """Sentry knows a task only by its code and the comment API wants the numeric id; the created
    comment must come back, because Sentry stores its id on the note to be able to edit it."""
    client = FakeClient()

    comment = post_task_comment(client, "PLT-500", "Ivan wrote:\n\n> hello")

    assert client.tasks_fetched == ["PLT-500"]
    assert client.comments == [(TASK_ID, "Ivan wrote:\n\n> hello")]
    assert comment["id"] == 901


def test_post_task_comment_skips_the_lookup_when_the_id_is_already_known() -> None:
    """The link path already holds the task id in `ExternalIssue.metadata`, so fetching the task
    again in the same request is a round trip for nothing."""
    client = FakeClient()

    post_task_comment(client, "PLT-500", "linked", task_id=TASK_ID)

    assert client.tasks_fetched == []
    assert client.comments == [(TASK_ID, "linked")]


def test_edit_task_comment_rewrites_the_comment_it_is_given() -> None:
    """An edited Sentry note must amend the Jaga comment it created, not add a second one."""
    client = FakeClient()

    edit_task_comment(client, "PLT-500", 901, "Ivan wrote:\n\n> hello again")

    assert client.updated_comments == [(901, TASK_ID, "Ivan wrote:\n\n> hello again")]
    assert client.comments == []


# --- assignee sync, Sentry -> Jaga -----------------------------------------


def test_apply_assignee_sync_writes_the_uuid_of_the_first_email_jaga_knows() -> None:
    """Sentry allows a user several addresses and cannot know which one Jaga has them under, so
    each is tried in turn; the `fieldId` comes off the task's own cell, at no extra call."""
    client = FakeClient()

    result = apply_assignee_sync(
        client, "PLT-500", ["unknown@example.com", "ivanov@example.com"], assign=True
    )

    assert result == "assigned"
    assert client.assignees_set == [(TASK_ID, 103, ["uuid-1"])]
    assert client.people_resolved == ["unknown@example.com", "ivanov@example.com"]


def test_apply_assignee_sync_stops_at_the_first_address_that_resolves() -> None:
    """A hit must not go on asking Jaga about the addresses behind it."""
    client = FakeClient()

    apply_assignee_sync(
        client, "PLT-500", ["ivanov@example.com", "petrov@example.com"], assign=True
    )

    assert client.people_resolved == ["ivanov@example.com"]
    assert client.assignees_set == [(TASK_ID, 103, ["uuid-1"])]


def test_apply_assignee_sync_clears_the_field_when_unassigning() -> None:
    """Verified against a live instance: an empty list is how Jaga is told "nobody"."""
    client = FakeClient()

    result = apply_assignee_sync(client, "PLT-500", ["ivanov@example.com"], assign=False)

    assert result == "unassigned"
    assert client.assignees_set == [(TASK_ID, 103, [])]
    assert client.people_resolved == [], "unassigning asks Jaga about nobody"


def test_apply_assignee_sync_leaves_the_task_alone_for_a_user_jaga_never_heard_of() -> None:
    """Someone who works in Sentry but not in Jaga is the normal case, not an error: clearing the
    assignee would take a real person off a real task."""
    client = FakeClient()

    result = apply_assignee_sync(client, "PLT-500", ["ghost@example.com"], assign=True)

    assert result == "no_such_user"
    assert client.assignees_set == [], "the task keeps whoever it had"


def test_apply_assignee_sync_is_a_no_op_when_the_type_has_no_assignee_attribute() -> None:
    """Not every task type carries `task.assignee_uuid`, and there is then nothing to PATCH."""
    client = FakeClient(task=raw_task(with_assignee=False))

    result = apply_assignee_sync(client, "PLT-500", ["ivanov@example.com"], assign=True)

    assert result == "no_attribute"
    assert client.assignees_set == []
    assert client.people_resolved == [], "no attribute means not even a lookup is worth paying for"


def test_assignee_field_id_is_read_off_the_task_itself() -> None:
    """A task's cells carry their own `fieldId`, which is what spares the sync a round trip to the
    task type."""
    assert assignee_field_id(raw_task()) == 103
    assert assignee_field_id(raw_task(with_assignee=False)) is None


def test_a_blank_assignee_value_removes_the_cell_instead_of_sending_it_empty() -> None:
    """An empty list is not "no opinion" to Jaga but the instruction to CLEAR the assignee — which
    is exactly how `apply_assignee_sync` unassigns — so the honest payload carries no cell."""
    client = FakeClient()
    create_task_from_form(
        client,
        {
            "project": "1",
            "issue_type": "10",
            "title": "Login is broken",
            "attr_103": ["   "],  # a whitespace-only value survives `form_data_to_attributes`
        },
    )

    assert client.created is not None
    assert _cell(client.created["attributes"], 103) is None
    assert client.people_resolved == []
