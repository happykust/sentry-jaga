import logging
from typing import Any

import pytest

from sentry_jaga.client.exceptions import JagaError
from sentry_jaga.client.models import Attribute, Project, TaskRef, TaskType
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
    MIN_QUERY_LENGTH,
    NoProjectsError,
    build_create_config,
    build_link_config,
    create_task_from_form,
    get_task_summary,
    resolve_task_id,
    search_task_summaries,
    status_comment,
)

# The attributes of a real Jaga task type ("Стандарт"), as the live instance reports them —
# including the two Jaga demands inside `attributes` even though they are already in the URL.
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
# A dictionary-backed attribute — the only reference kind that lists itself.
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

USERS = [("uuid-1", "Ivanov Ivan"), ("uuid-2", "Petrov Petr")]
LABELS = [("7", "backend"), ("8", "frontend")]


class FakeClient:
    """A stand-in JagaClient: same methods, records the calls."""

    def __init__(
        self,
        projects: list[Project] | None = None,
        task_types: list[TaskType] | None = None,
        attributes: list[Attribute] | None = None,
        task_types_by_project: dict[int, list[TaskType]] | None = None,
    ) -> None:
        self._projects = projects if projects is not None else [Project(1, "Platform", "PLT")]
        self._task_types = task_types if task_types is not None else [TaskType(10, "Bug")]
        self._task_types_by_project = task_types_by_project
        self._attributes = attributes if attributes is not None else list(REAL_ATTRIBUTES)
        self.created: dict[str, Any] | None = None
        self.comments: list[tuple[int, str]] = []
        self.searches: list[tuple[int, str]] = []
        self.attributes_requested: list[tuple[int, int]] = []
        self.users_requested: list[int] = []
        self.labels_requested = 0

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

    def get_labels(self) -> list[tuple[str, str]]:
        self.labels_requested += 1
        return LABELS

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
                {"fieldId": 100, "value": "Login is broken", "objectTypeNameM": TITLE_OBJECT_TYPE}
            ],
        }

    def search_tasks(self, project_id: int, text: str, *, size: int = 20) -> list[TaskRef]:
        self.searches.append((project_id, text))
        return [TaskRef(id=5, code="PLT-5", title="Login is broken")]

    def create_comment(self, task_id: int, content: str) -> None:
        self.comments.append((task_id, content))


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
    """`task.project_id` and `task.type_id` are required attributes of the task type, but the
    `project` / `issue_type` selects already carry them. Rendering them too would show the user
    two "Space" boxes — one of which does not drive the cascade. They go into the payload
    instead (see `test_create_task_from_form_injects_space_and_type`).

    The author and the creation date are Jaga's to fill; asking the user for them is nonsense.

    Dropped on purpose, and not as collateral of the "cannot render this" rule: both are
    `required`, so falling through to that rule would log a warning blaming two fields the
    plugin actually submits — right in the log of whoever is debugging a failed create.
    """
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
    """Priority is a reference with no dictionary: we cannot list its values, so it is left out
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
    """A task type without assignees or labels must not pay an HTTP request per form render —
    and the create form re-renders on every keystroke of the cascade."""
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
    """Switching the space while an `issue_type` from the previous one is still stuck.

    On `updatesForm`, Sentry resends EVERY form field, not only the one that changed: after
    `project` is switched, `params` arrives carrying the `issue_type` of the OLD space.
    Taking it at face value means a 404 from Jaga or, worse, a silently created task of a
    foreign type. We expect a fall back to the first type of the new space.
    """
    client = FakeClient(
        projects=[Project(1, "Platform", "PLT"), Project(2, "Billing", "BIL")],
        task_types_by_project={
            1: [TaskType(10, "Bug"), TaskType(11, "Task")],
            2: [TaskType(20, "Incident"), TaskType(21, "Request")],
        },
    )

    # The user switched the space to 2, while issue_type=10 is left over from space 1.
    fields = build_create_config(client, {"project": "2", "issue_type": "10"}, "t", "d")
    by_name = _by_name(fields)

    assert by_name["issue_type"]["default"] == "20"
    assert by_name["issue_type"]["choices"] == [("20", "Incident"), ("21", "Request")]
    # And the attributes were requested for the type of the NEW space, not a foreign one.
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
    """REGRESSION. Jaga answers 500 to a create whose `attributes` omit `task.project_id` and
    `task.type_id` — "Поле "Пространство" обязательно для заполнения" — even though both ids
    are already in the URL of `POST /v1/task/createByTaskType/{project}/{type}`.

    Neither has a form field of its own (the cascade selects play that role), so the payload
    built from `attr_*` keys alone can never contain them, and EVERY create from Sentry used
    to fail. Both cells must be there, carrying the ids the user picked, marked as references.
    """
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
    """The exact `data` an alert rule hands to `create_issue()` must produce a titled task.

    `sentry.rules.actions.integrations.create_ticket.utils.create_issue` takes the action's
    saved config (space, task type, and whatever else the admin picked), overwrites `title`
    and `description` with the event's own, and calls `installation.create_issue(data)`. The
    space/type keys come from the saved rule; the title and the body come from the event and
    were NOT in the rule config at all.

    This is the whole of what a ticket rule does on our side — if the title does not land in
    the Jaga title cell, every task filed by a rule is nameless.
    """
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

    # And the ExternalIssue Sentry records for the rule-created task is named after the event.
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


def test_create_task_from_form_sends_assignees_and_labels_as_references() -> None:
    client = FakeClient()
    create_task_from_form(
        client,
        {
            "project": "1",
            "issue_type": "10",
            "title": "Login is broken",
            "attr_103": ["uuid-1", "uuid-2"],
            "attr_104": ["7"],
        },
    )

    assert client.created is not None
    payload = client.created["attributes"]

    assignee = _cell(payload, 103)
    assert assignee is not None
    assert assignee["value"] == ["uuid-1", "uuid-2"]
    assert assignee["referenceValue"] is True

    label = _cell(payload, 104)
    assert label is not None
    assert label["value"] == ["7"]
    assert label["referenceValue"] is True


def test_create_task_from_form_rejects_empty_form() -> None:
    """An empty form is refused before the injected cells make the payload look non-empty."""
    with pytest.raises(JagaError):
        create_task_from_form(FakeClient(), {"project": "1", "issue_type": "10"})


def test_create_task_from_form_returns_task_id_in_metadata() -> None:
    """`metadata` travels into `ExternalIssue`. Without `task_id`, every resolve would look
    the task up by code again — even though Jaga has just returned the id itself."""
    result = create_task_from_form(
        FakeClient(), {"project": "1", "issue_type": "10", "title": "Login is broken"}
    )

    assert result["metadata"] == {"task_id": 500}


def test_created_task_needs_no_lookup_to_resolve_its_id() -> None:
    """End-to-end check of what I4 is for: the id of a created task resolves without a
    round trip to Jaga."""
    client = FakeClient()
    created = create_task_from_form(
        client, {"project": "1", "issue_type": "10", "title": "Login is broken"}
    )

    assert resolve_task_id(client, created["key"], created["metadata"]) == 500


def test_warns_when_no_system_attribute_recognised(caplog: pytest.LogCaptureFixture) -> None:
    """The title/description mnemonics hold on the instance we probed, but they are not a
    documented contract. If not a single one is recognised, the miss must not degrade
    silently: the form would come out with no title and no Sentry context."""
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


def test_link_config_searches_when_query_given() -> None:
    fields = build_link_config(FakeClient(), {"project": "1", "query": "login"})
    by_name = _by_name(fields)

    assert by_name["query"]["updatesForm"] is True
    assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]


def test_link_config_without_query_has_no_choices() -> None:
    by_name = _by_name(build_link_config(FakeClient(), {"project": "1"}))
    assert by_name["externalIssue"]["choices"] == []


def test_link_config_does_not_search_below_min_query_length() -> None:
    """`updatesForm` re-fetches the config on EVERY keystroke, Sentry has no debounce, and an
    external package cannot ship its own JS. A minimum query length is the only server-side
    brake: below `MIN_QUERY_LENGTH` we do not call Jaga at all."""
    client = FakeClient()
    short = "l" * (MIN_QUERY_LENGTH - 1)

    by_name = _by_name(build_link_config(client, {"project": "1", "query": short}))

    assert client.searches == []
    assert by_name["externalIssue"]["choices"] == []


def test_link_config_searches_from_min_query_length() -> None:
    client = FakeClient()
    enough = "l" * MIN_QUERY_LENGTH

    by_name = _by_name(build_link_config(client, {"project": "1", "query": enough}))

    assert client.searches == [(1, enough)]
    assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]


def test_link_config_ignores_whitespace_only_query() -> None:
    client = FakeClient()
    build_link_config(client, {"project": "1", "query": "   "})
    assert client.searches == []


def test_link_config_help_states_the_minimum_query_length() -> None:
    by_name = _by_name(build_link_config(FakeClient(), {"project": "1"}))
    assert str(MIN_QUERY_LENGTH) in by_name["query"]["help"]


# --- the two shapes of the link form: with and without the search endpoint ---------------


def test_link_config_with_a_search_url_builds_an_async_select() -> None:
    """Given a URL, the task picker becomes a live autocomplete.

    `url` on a select field is the whole mechanism: Sentry's frontend (`getFieldProps`) turns
    the field async only when it sees one, and then calls the endpoint — debounced — as the
    user types. The `query` text box exists purely to work around not having this, so it must
    be gone; leaving it would mean two search boxes for one search.
    """
    client = FakeClient()

    fields = build_link_config(client, {"project": "1"}, search_url="/extensions/jaga/search/o/1/")
    by_name = _by_name(fields)

    external_issue = by_name["externalIssue"]
    assert external_issue["url"] == "/extensions/jaga/search/o/1/"
    assert external_issue["type"] == "select"
    assert "query" not in by_name

    # The space select stays, and stays `updatesForm`: its value is what the frontend sends to
    # the endpoint as `?project=`, and Jaga cannot search without a space.
    assert by_name["project"]["updatesForm"] is True

    # Nothing is searched while the form is merely being rendered — that is the endpoint's job.
    assert client.searches == []


def test_link_config_without_a_search_url_keeps_the_updates_form_search() -> None:
    """The endpoint is only mounted if the admin set `ROOT_URLCONF`. Without it the form must
    still work — the old `query` + `updatesForm` behaviour, not a dead select."""
    by_name = _by_name(build_link_config(FakeClient(), {"project": "1", "query": "login"}))

    assert "url" not in by_name["externalIssue"]
    assert by_name["query"]["updatesForm"] is True
    assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]


def test_get_task_summary() -> None:
    summary = get_task_summary(FakeClient(), "PLT-500")
    assert summary["key"] == "PLT-500"
    assert summary["title"] == "Login is broken"
    assert summary["metadata"] == {"task_id": 500}


def test_search_task_summaries() -> None:
    assert search_task_summaries(FakeClient(), 1, "login") == [
        {"key": "PLT-5", "title": "Login is broken"}
    ]


def test_search_task_summaries_without_query_is_empty() -> None:
    assert search_task_summaries(FakeClient(), 1, "") == []


def test_status_comment_distinguishes_resolution() -> None:
    assert "resolved" in status_comment(is_resolved=True).lower()
    assert "reopened" in status_comment(is_resolved=False).lower()


def test_resolve_task_id_prefers_metadata() -> None:
    assert resolve_task_id(FakeClient(), "PLT-500", {"task_id": 77}) == 77


def test_resolve_task_id_falls_back_to_lookup_by_code() -> None:
    assert resolve_task_id(FakeClient(), "PLT-500", {}) == 500
