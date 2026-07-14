import json
import logging
from typing import Any

import pytest
import responses

from sentry_jaga.client.api import MAX_MEMBER_PAGES, JagaClient
from sentry_jaga.client.auth import InMemoryCache
from sentry_jaga.client.exceptions import (
    JagaAuthError,
    JagaError,
    JagaNotFoundError,
    JagaServerError,
)

BASE = "https://jaga.example.com"
API = f"{BASE}/external-api"

AUTH_OK: dict[str, Any] = {
    "accessToken": "at1",
    "refreshToken": "rt1",
    "expiresAt": "2099-01-01T00:00:00Z",
    "id": 1,
    "email": "bot@example.com",
    "fullName": "Bot",
}


@pytest.fixture
def client() -> JagaClient:
    return JagaClient(instance_url=BASE, email="bot@example.com", password="secret")


def _mock_login() -> None:
    responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)


@responses.activate
def test_login_returns_token(client: JagaClient) -> None:
    _mock_login()
    token = client.login()
    assert token.access_token == "at1"
    assert responses.calls[0].request.body is not None


@responses.activate
def test_refresh_sends_refresh_token_and_returns_new_token(client: JagaClient) -> None:
    """A token renewal goes to /v1/auth/refresh with a {"refreshToken": ...} body."""
    responses.add(
        responses.POST,
        f"{API}/v1/auth/refresh",
        json={
            "accessToken": "at2",
            "refreshToken": "rt2",
            "expiresAt": "2099-06-01T12:00:00Z",
            "id": 1,
            "email": "bot@example.com",
            "fullName": "Bot",
        },
        status=200,
    )
    token = client.refresh("rt1")

    assert (token.access_token, token.refresh_token) == ("at2", "rt2")
    assert token.expires_at.isoformat() == "2099-06-01T12:00:00+00:00"
    assert json.loads(responses.calls[-1].request.body) == {"refreshToken": "rt1"}


@responses.activate
def test_requests_send_bearer_token(client: JagaClient) -> None:
    _mock_login()
    responses.add(
        responses.GET,
        f"{API}/v1/project/list/my",
        json={"content": [], "totalPages": 0, "pageNumber": 0, "totalElements": 0},
        status=200,
    )
    client.get_projects()
    assert responses.calls[-1].request.headers["Authorization"] == "Bearer at1"


@responses.activate
def test_get_projects_unwraps_page(client: JagaClient) -> None:
    _mock_login()
    responses.add(
        responses.GET,
        f"{API}/v1/project/list/my",
        json={
            "content": [
                {"id": 1, "title": "Platform", "code": "PLT"},
                {"id": 2, "title": "Billing", "code": "BIL"},
            ],
            "totalPages": 1,
            "pageNumber": 0,
            "totalElements": 2,
        },
        status=200,
    )
    projects = client.get_projects()
    assert [p.code for p in projects] == ["PLT", "BIL"]


@responses.activate
def test_get_projects_reads_every_page(client: JagaClient) -> None:
    """More spaces than fit on one page — read up to `totalPages` instead of stopping at the
    first one."""
    _mock_login()
    responses.add(
        responses.GET,
        f"{API}/v1/project/list/my",
        json={
            "content": [{"id": 1, "title": "Platform", "code": "PLT"}],
            "totalPages": 2,
            "pageNumber": 0,
            "totalElements": 2,
        },
        status=200,
    )
    responses.add(
        responses.GET,
        f"{API}/v1/project/list/my",
        json={
            "content": [{"id": 2, "title": "Billing", "code": "BIL"}],
            "totalPages": 2,
            "pageNumber": 1,
            "totalElements": 2,
        },
        status=200,
    )

    assert [p.code for p in client.get_projects()] == ["PLT", "BIL"]

    pages = [
        call.request.url.split("page=")[1].split("&")[0]
        for call in responses.calls
        if "/v1/project/list/my" in call.request.url
    ]
    assert pages == ["0", "1"]


@responses.activate
def test_get_projects_is_cached(client: JagaClient) -> None:
    """A repeat call takes the list from the cache: the link form pulls it on every key."""
    _mock_login()
    responses.add(
        responses.GET,
        f"{API}/v1/project/list/my",
        json={
            "content": [{"id": 1, "title": "Platform", "code": "PLT"}],
            "totalPages": 1,
            "pageNumber": 0,
            "totalElements": 1,
        },
        status=200,
    )

    first = client.get_projects()
    second = client.get_projects()

    assert [p.code for p in first] == [p.code for p in second] == ["PLT"]
    list_calls = [c for c in responses.calls if "/v1/project/list/my" in c.request.url]
    assert len(list_calls) == 1


@responses.activate
def test_projects_cache_is_shared_between_clients() -> None:
    """The cache is injected from outside (the Django cache in production), so it outlives a
    single HTTP request."""
    cache = InMemoryCache()
    _mock_login()
    responses.add(
        responses.GET,
        f"{API}/v1/project/list/my",
        json={
            "content": [{"id": 1, "title": "Platform", "code": "PLT"}],
            "totalPages": 1,
            "pageNumber": 0,
            "totalElements": 1,
        },
        status=200,
    )

    def make() -> JagaClient:
        return JagaClient(
            instance_url=BASE, email="bot@example.com", password="secret", cache=cache
        )

    assert [p.code for p in make().get_projects()] == ["PLT"]
    assert [p.code for p in make().get_projects()] == ["PLT"]

    list_calls = [c for c in responses.calls if "/v1/project/list/my" in c.request.url]
    assert len(list_calls) == 1


@responses.activate
def test_get_task_types(client: JagaClient) -> None:
    _mock_login()
    responses.add(
        responses.GET,
        f"{API}/v1/project/1/taskType",
        json=[{"id": 10, "typeName": "Bug"}, {"id": 11, "typeName": "Task"}],
        status=200,
    )
    types = client.get_task_types(1)
    assert [(t.id, t.name) for t in types] == [(10, "Bug"), (11, "Task")]


@responses.activate
def test_get_task_type_attributes_flattens_groups(client: JagaClient) -> None:
    _mock_login()
    responses.add(
        responses.GET,
        f"{API}/v1/project/1/taskType/10",
        json={
            "id": 10,
            "typeName": "Bug",
            "modulesEnabled": [],
            "groups": [
                {
                    "title": "General",
                    "orderNum": 0,
                    "attributes": [
                        {
                            "id": 90,
                            "name": "Space",
                            "objectTypeNameM": "task.project_id",
                            "required": True,
                        },
                        {
                            "id": 100,
                            "name": "Title",
                            "objectTypeNameM": "task.task_title",
                            "required": True,
                        },
                        {"id": 101, "name": "Description", "objectTypeNameM": "task.content"},
                    ],
                },
                {
                    "title": "Other",
                    "orderNum": 1,
                    "attributes": [
                        {
                            "id": 103,
                            "name": "Assignees",
                            "objectTypeNameM": "task.assignee_uuid",
                            "multiple": True,
                        },
                        {
                            "id": 110,
                            "name": "Severity",
                            "objectTypeNameM": "task.flex_severity",
                            "dictionaryId": 55,
                        },
                    ],
                },
            ],
        },
        status=200,
    )
    attrs = client.get_task_type_attributes(1, 10)

    assert [a.id for a in attrs] == [90, 100, 101, 103, 110]
    by_id = {a.id: a for a in attrs}
    assert by_id[90].required is True
    assert by_id[103].multiple is True
    assert by_id[110].dictionary_id == 55


# --- value sources for the reference attributes with no dictionary ---------


def _matrix_url(space_id: int) -> str:
    return f"{API}/v1/team/userRoles/applications/JAGA/projects/{space_id}"


def _matrix_page(users: list[dict[str, Any]], total_pages: int = 1) -> dict[str, Any]:
    """One page of the user-role matrix: roles, each carrying the users that hold it."""
    return {
        "content": [{"rolesList": [], "usersRoles": [{"user": u, "roles": []} for u in users]}],
        "totalPages": total_pages,
        "pageNumber": 0,
        "totalElements": len(users),
    }


@responses.activate
def test_get_space_users_reads_the_role_matrix_and_returns_emails(client: JagaClient) -> None:
    """The select's value is the EMAIL, not the person UUID the attribute finally stores.

    The matrix is the only member list that works on a live instance, and it carries no UUID —
    those cost one call each, which the form cannot afford for every member. So the email travels
    and the UUID is resolved for whoever is picked. See `resolve_assignee_cells`.
    """
    _mock_login()
    responses.add(
        responses.GET,
        _matrix_url(1),
        json=_matrix_page(
            [
                {
                    "id": 365474,  # the TEAM id — not the Core id, and nothing reads it
                    "displayName": "Ivanov Ivan",
                    "email": "ivanov@example.com",
                    "isGroup": False,
                    "type": "USER",
                }
            ]
        ),
        status=200,
    )

    assert client.get_space_users(1) == [("ivanov@example.com", "Ivanov Ivan")]


@responses.activate
def test_get_space_users_drops_groups_and_the_email_less(client: JagaClient) -> None:
    """A group cannot be the executor of a task, and a member with no email can never be resolved
    to a UUID — offering either would only build a form whose submit fails."""
    _mock_login()
    responses.add(
        responses.GET,
        _matrix_url(1),
        json=_matrix_page(
            [
                {
                    "id": 1,
                    "displayName": "Person",
                    "email": "p@e.com",
                    "isGroup": False,
                    "type": "USER",
                },
                {
                    "id": 2,
                    "displayName": "A group",
                    "email": "g@e.com",
                    "isGroup": True,
                    "type": "GROUP",
                },
                {
                    "id": 3,
                    "displayName": "No email",
                    "email": None,
                    "isGroup": False,
                    "type": "USER",
                },
            ]
        ),
        status=200,
    )

    assert client.get_space_users(1) == [("p@e.com", "Person")]


@responses.activate
def test_get_space_users_dedupes_a_member_who_holds_two_roles(client: JagaClient) -> None:
    """The matrix is a page of ROLES, each with its holders — so one person appears once per role
    they have. Listing them twice would put them twice in the dropdown."""
    _mock_login()
    person = {
        "id": 1,
        "displayName": "Two hats",
        "email": "both@example.com",
        "isGroup": False,
        "type": "USER",
    }
    responses.add(
        responses.GET,
        _matrix_url(1),
        json={
            "content": [
                {"rolesList": [], "usersRoles": [{"user": person}]},
                {"rolesList": [], "usersRoles": [{"user": person}]},
            ],
            "totalPages": 1,
        },
        status=200,
    )

    assert client.get_space_users(1) == [("both@example.com", "Two hats")]


@responses.activate
def test_get_space_users_reads_every_page(client: JagaClient) -> None:
    """The 101st member of a space must be offered too."""
    _mock_login()
    responses.add(
        responses.GET,
        _matrix_url(1),
        json=_matrix_page(
            [
                {
                    "id": 1,
                    "displayName": "One",
                    "email": "one@e.com",
                    "isGroup": False,
                    "type": "USER",
                }
            ],
            total_pages=2,
        ),
        status=200,
    )
    responses.add(
        responses.GET,
        _matrix_url(1),
        json=_matrix_page(
            [
                {
                    "id": 2,
                    "displayName": "Two",
                    "email": "two@e.com",
                    "isGroup": False,
                    "type": "USER",
                }
            ],
            total_pages=2,
        ),
        status=200,
    )

    assert client.get_space_users(1) == [("one@e.com", "One"), ("two@e.com", "Two")]


@responses.activate
def test_get_space_users_falls_back_to_the_documented_endpoint_when_the_matrix_errors(
    client: JagaClient,
) -> None:
    """`applicationMnemo` is a guess ("JAGA" is what the live instance takes, and the spec names
    no value at all). An instance that calls its application something else answers 404 — and the
    documented endpoint, dead as it is here, is then the only thing left to try."""
    _mock_login()
    responses.add(responses.GET, _matrix_url(1), json={"error": "nope"}, status=404)
    responses.add(
        responses.GET,
        f"{API}/v1/project/getUserProfileDtos/1",
        json=[
            {
                "id": 1,
                "personUuid": "uuid-ok",
                "email": "fallback@example.com",
                "displayName": "From the old endpoint",
                "canBeAssign": True,
                "isBlocked": False,
            },
            {
                "id": 2,
                "personUuid": "uuid-blocked",
                "email": "blocked@example.com",
                "displayName": "Blocked",
                "canBeAssign": True,
                "isBlocked": True,
            },
        ],
        status=200,
    )

    assert client.get_space_users(1) == [("fallback@example.com", "From the old endpoint")]


@responses.activate
def test_get_space_users_takes_an_empty_matrix_at_face_value(client: JagaClient) -> None:
    """A space really can have nobody in it. Falling back on an EMPTY (as opposed to failing)
    matrix would re-introduce the very bug this replaced: `getUserProfileDtos` answers 200 with
    [] for every space on the live instance, so a second empty answer would hide the first."""
    _mock_login()
    responses.add(responses.GET, _matrix_url(1), json=_matrix_page([]), status=200)

    assert client.get_space_users(1) == []
    assert not [call for call in responses.calls if "getUserProfileDtos" in call.request.url], (
        "the dead endpoint must not be called when the matrix answered properly"
    )


# --- resolving one person: the only door to a UUID -------------------------


@responses.activate
def test_find_person_by_email_returns_all_three_identifiers(client: JagaClient) -> None:
    """Jaga keeps two unrelated numeric ids for one human, plus the UUID. `Person` names each."""
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/team/userProfile/findByMailOrName",
        json={
            "coreId": 193688,
            "teamId": 365474,
            "uuid": "aea8739a-c7dc-49c3-b1e5-5bc909ef364f",
            "mail": "ivanov@example.com",
            "fullName": "Ivanov Ivan",
        },
        status=200,
    )

    person = client.find_person_by_email("ivanov@example.com")

    assert person is not None
    assert person.uuid == "aea8739a-c7dc-49c3-b1e5-5bc909ef364f"
    assert person.core_id == 193688
    assert person.team_id == 365474
    assert person.email == "ivanov@example.com"
    assert person.name == "Ivanov Ivan"


@responses.activate
def test_find_person_by_email_treats_400_as_not_found(client: JagaClient) -> None:
    """Jaga answers an unknown email with 400 and `NotFoundException` — a 400 that means "no such
    user". A Sentry user is under no obligation to exist in Jaga, so this is not an error."""
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/team/userProfile/findByMailOrName",
        json={"error": '{"status":400,"message":"User with email x@e.com does not exists"}'},
        status=400,
    )

    assert client.find_person_by_email("x@e.com") is None


@responses.activate
def test_find_person_by_email_still_raises_on_a_real_failure(client: JagaClient) -> None:
    """A 500 is Jaga breaking, and must not be read as "nobody"."""
    _mock_login()
    responses.add(
        responses.POST, f"{API}/v1/team/userProfile/findByMailOrName", json={}, status=500
    )

    with pytest.raises(JagaError):
        client.find_person_by_email("x@e.com")


@responses.activate
def test_find_person_by_email_raises_on_a_400_that_does_not_say_not_found(
    client: JagaClient,
) -> None:
    """THE important one. Jaga says "no such user" with a 400, so it is tempting to read every 400
    as "nobody" — and that would swallow our OWN bugs: a renamed field, a moved route, a WAF in
    front of the API. Each would come back as a confident, hour-long cached "that person does not
    exist" about somebody who is standing right there in the dropdown. An unexplained 400 must be
    heard."""
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/team/userProfile/findByMailOrName",
        json={"error": "Required request parameter 'searchText' is not present"},
        status=400,
    )

    with pytest.raises(JagaError):
        client.find_person_by_email("ivanov@example.com")


@responses.activate
def test_find_person_by_email_refuses_a_person_it_did_not_ask_for(client: JagaClient) -> None:
    """The endpoint is find-by-mail-OR-NAME, and nothing in its contract promises an exact match.
    A fuzzy hit would hand back a different human — whom we would then put on a real task, with
    total confidence and no way to notice."""
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/team/userProfile/findByMailOrName",
        json={
            "coreId": 1,
            "teamId": 2,
            "uuid": "uuid-of-someone-else",
            "mail": "ivanova@example.com",
            "fullName": "Ivanova Irina",
        },
        status=200,
    )

    assert client.find_person_by_email("ivanov@example.com") is None


@responses.activate
def test_find_person_by_email_caches_the_answer(client: JagaClient) -> None:
    """A UUID is immutable, and this runs on every assignment and every create."""
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/team/userProfile/findByMailOrName",
        json={"coreId": 1, "teamId": 2, "uuid": "u", "mail": "a@e.com", "fullName": "A"},
        status=200,
    )

    first = client.find_person_by_email("a@e.com")
    second = client.find_person_by_email("a@e.com")

    assert first == second
    lookups = [c for c in responses.calls if "findByMailOrName" in c.request.url]
    assert len(lookups) == 1, "the second lookup must come from the cache"


@responses.activate
def test_find_person_by_email_caches_a_miss_too(client: JagaClient) -> None:
    """A Sentry user with no Jaga account must not cost a round trip on every single assignment.

    Only a miss Jaga *explained* is cached — hence the real not-found body here rather than a bare
    400. An unexplained 400 raises and is cached as nothing; see the test above.
    """
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/team/userProfile/findByMailOrName",
        json={"error": '{"exception":"...NotFoundException","message":"User does not exists"}'},
        status=400,
    )

    assert client.find_person_by_email("nobody@example.com") is None
    assert client.find_person_by_email("nobody@example.com") is None

    lookups = [c for c in responses.calls if "findByMailOrName" in c.request.url]
    assert len(lookups) == 1, "the miss must be cached, not re-asked"


@responses.activate
def test_find_person_by_email_does_not_call_jaga_for_a_blank_email(client: JagaClient) -> None:
    """`responses` is active but NOTHING is registered, so any HTTP call at all fails the test.

    The decorator is the whole point: without it `responses` does not patch `requests`, and a
    regression here would send a real request to the configured instance — which would then fail,
    or not, depending on DNS. That is not a test.
    """
    assert client.find_person_by_email("   ") is None
    assert len(responses.calls) == 0


# --- writing the assignee --------------------------------------------------


@responses.activate
def test_set_task_assignees_patches_the_attribute(client: JagaClient) -> None:
    """The assignee is an EAV attribute, not a task role: `PUT /v1/taskRole/task/{id}/executor` is
    what the spec offers and it 404s on the live instance (no handler at all). `PATCH /v1/task/
    {id}` takes the same cell the create builds, and the value travels as a LIST."""
    _mock_login()
    responses.add(responses.PATCH, f"{API}/v1/task/1703944", json={"id": 1703944}, status=200)

    client.set_task_assignees(1703944, 867868, ["uuid-a"])

    body = json.loads(responses.calls[-1].request.body)
    assert body == {
        "fieldId": 867868,
        "value": ["uuid-a"],
        "referenceValue": True,
        "addInfo": {},
        "objectTypeNameM": "task.assignee_uuid",
    }


@responses.activate
def test_set_task_assignees_clears_with_an_empty_list(client: JagaClient) -> None:
    """Unassigning in Sentry has to reach Jaga. Verified against a live instance: an empty list
    empties the field."""
    _mock_login()
    responses.add(responses.PATCH, f"{API}/v1/task/7", json={}, status=200)

    client.set_task_assignees(7, 867868, [])

    assert json.loads(responses.calls[-1].request.body)["value"] == []


@responses.activate
def test_get_labels_returns_ids(client: JagaClient) -> None:
    """The value of `task.label_id` is the label id; labels have no `/v1/listRef` dictionary,
    so they come from a POST search with an empty filter."""
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/labels/getPage",
        json={
            "content": [
                {"id": 7, "uuid": "u7", "color": "#fff", "name": "backend", "projects": []},
                {"id": 8, "uuid": "u8", "color": "#000", "name": "frontend", "projects": []},
            ],
            "totalPages": 1,
            "pageNumber": 0,
            "totalElements": 2,
        },
        status=200,
    )

    assert client.get_labels() == [("7", "backend"), ("8", "frontend")]

    sent = json.loads(responses.calls[-1].request.body)
    assert sent == {"searchText": "", "order": "ASC", "orderBy": "name"}


@responses.activate
def test_get_or_create_label_returns_the_id_of_the_label(client: JagaClient) -> None:
    """`/v1/labels/list` is a get-or-create, despite the name: it answers with the labels named
    in the body and creates the ones Jaga does not have yet. Verified against a live instance."""
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/labels/list",
        json={
            "labels": [
                {"id": 17834, "uuid": "u1", "name": "sentry", "color": "#8348FC1F", "projects": []}
            ]
        },
        status=200,
    )

    assert client.get_or_create_label("sentry") == 17834
    assert json.loads(responses.calls[-1].request.body) == {"names": ["sentry"]}


@responses.activate
def test_get_or_create_label_without_a_label_in_the_answer_raises(client: JagaClient) -> None:
    """The endpoint is a get-or-create: an empty `labels` means it neither found nor made the
    label. There is no id to put on the task, so say so instead of blowing up on an IndexError."""
    _mock_login()
    responses.add(responses.POST, f"{API}/v1/labels/list", json={"labels": []}, status=200)

    with pytest.raises(JagaError):
        client.get_or_create_label("sentry")


@responses.activate
def test_attach_file_uploads_multipart_and_binds_the_file_to_the_task(client: JagaClient) -> None:
    """`projectId` is mandatory (Jaga files attachments under a space) and `taskId` is what binds
    the file to the task — despite the endpoint's "without binding to entity" summary. Verified
    against a live instance."""
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/attacher/file/create",
        json={
            "id": 1901762,
            "attachUser": 193688,
            "attachName": "sentry-event.json",
            "attachType": "json",
            "attachSize": 50,
            "attachPath": "/x",
            "originalName": "sentry-event.json",
            "createTs": "2026-07-01T10:00:00Z",
            "isDeleted": False,
        },
        status=200,
    )

    attachment = client.attach_file(
        space_id=11361,
        task_id=1703944,
        filename="sentry-event.json",
        content=b'{"event_id": "abc"}',
        content_type="application/json",
    )
    assert attachment["id"] == 1901762

    request = responses.calls[-1].request
    assert "projectId=11361" in request.url
    assert "taskId=1703944" in request.url
    # `requests` builds the multipart body — and its boundary — from `files=`. A Content-Type of
    # our own would destroy the boundary, so the header must be the one it generated.
    assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    body = bytes(request.body)
    assert b'name="file"; filename="sentry-event.json"' in body
    assert b'{"event_id": "abc"}' in body


@responses.activate
def test_get_dictionary_values(client: JagaClient) -> None:
    _mock_login()
    responses.add(
        responses.GET,
        f"{API}/v1/listRef/55/any",
        json={
            "name": "Priorities",
            "itemsMap": [],
            "items": [
                {"id": 1, "value": "High", "orderNum": 0},
                {"id": 2, "value": "Low", "orderNum": 1},
            ],
        },
        status=200,
    )
    assert client.get_dictionary_values(55) == [("1", "High"), ("2", "Low")]


@responses.activate
def test_create_task_posts_payload_and_returns_ref(client: JagaClient) -> None:
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/task/createByTaskType/1/10",
        json={
            "id": 500,
            "code": "PLT-500",
            "orderNum": 0,
            "statusId": 1,
            "statusModifierId": 1,
            "taskTypeId": 10,
            "updateTs": "2026-06-25T10:00:00Z",
            "statusTransitions": [],
            "colorIndicator": [],
            "timeInStatus": {},
            "attributes": [{"fieldId": 100, "value": "Login is broken", "referenceValue": False}],
        },
        status=200,
    )
    attributes = [
        {"fieldId": 100, "value": "Login is broken", "referenceValue": False, "addInfo": {}}
    ]
    ref = client.create_task(project_id=1, task_type_id=10, attributes=attributes)

    assert (ref.id, ref.code) == (500, "PLT-500")

    import json

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["statusModifier"] == 1
    assert sent["orderNum"] == 0
    assert sent["attachmentIds"] == []
    assert sent["attributes"] == attributes


@responses.activate
def test_get_task_by_code_returns_task(client: JagaClient) -> None:
    _mock_login()
    responses.add(
        responses.GET,
        f"{API}/v1/task/findExtendedWithFlexField/code/PLT-500",
        json={
            "id": 500,
            "code": "PLT-500",
            "statusId": 3,
            "statusModifierId": 2,
            "taskTypeId": 10,
            "attributes": [{"fieldId": 100, "value": "Login is broken", "referenceValue": False}],
        },
        status=200,
    )
    task = client.get_task_by_code("PLT-500")

    assert task["id"] == 500
    assert task["code"] == "PLT-500"
    assert task["statusId"] == 3
    assert task["attributes"] == [
        {"fieldId": 100, "value": "Login is broken", "referenceValue": False}
    ]


@responses.activate
def test_search_tasks_globally_searches_every_space(client: JagaClient) -> None:
    """The answer is shaped exactly as a live instance returns it — which is NOT what the spec
    says. The spec declares `array<TaskPageApiDto>`; the wire carries a single page object, and
    a task's title lives in its EAV `attributes` rather than in a `title` key."""
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/globalSearch/findTaskList",
        json={
            "content": [
                {
                    "id": 5,
                    "code": "PLT-5",
                    # The live instance returns the space of a found task as null — every time.
                    "projectId": None,
                    "projectCode": None,
                    "projectTitle": None,
                    "attributes": [
                        {
                            "fieldId": 100,
                            "value": "Login is broken",
                            "objectTypeNameM": "task.task_title",
                        }
                    ],
                }
            ],
            "totalPages": 1,
            "pageNumber": 0,
            "totalElements": 1,
        },
        status=200,
    )

    results = client.search_tasks_globally("login", size=5)
    assert [(r.id, r.code, r.title) for r in results] == [(5, "PLT-5", "Login is broken")]

    request = responses.calls[-1].request
    assert "query=login" in request.url
    assert "page=0" in request.url
    assert "size=5" in request.url
    # The body is the filter; an empty list means "no filter".
    assert json.loads(request.body) == []


@responses.activate
def test_search_tasks_globally_falls_back_to_the_code_when_a_task_has_no_title(
    client: JagaClient,
) -> None:
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/globalSearch/findTaskList",
        json={"content": [{"id": 5, "code": "PLT-5", "attributes": []}], "totalPages": 1},
        status=200,
    )

    assert [r.title for r in client.search_tasks_globally("login")] == ["PLT-5"]


@responses.activate
def test_create_comment(client: JagaClient) -> None:
    _mock_login()
    responses.add(responses.POST, f"{API}/v1/comment", json={"id": 1, "taskId": 500}, status=200)
    comment = client.create_comment(task_id=500, content="Resolved in Sentry")

    import json

    sent = json.loads(responses.calls[-1].request.body)
    assert sent == {
        "taskId": 500,
        "contentComment": "Resolved in Sentry",
        "attachIsPending": False,
    }
    # The created comment has to come back: Sentry reads its id out of the return value and
    # stores it on the note, and that id is the only way an edit can find the comment again.
    assert comment == {"id": 1, "taskId": 500}


@responses.activate
def test_create_comment_survives_an_empty_body(client: JagaClient) -> None:
    """Jaga answers a create with the whole comment — but a 200 with no body must not blow up
    the caller with `TypeError: dict(None)`. The comment is lost; the sync is not."""
    _mock_login()
    responses.add(responses.POST, f"{API}/v1/comment", body="", status=200)

    assert client.create_comment(task_id=500, content="hi") == {}


@responses.activate
def test_update_comment(client: JagaClient) -> None:
    """An edited Sentry note must rewrite the comment it created, not append a second one.
    Jaga's `CommentApiDto` wants `taskId` on the update too — the endpoint takes the same schema
    both ways — so the task id travels along with the comment id."""
    _mock_login()
    responses.add(responses.PUT, f"{API}/v1/comment", json={"id": 77, "taskId": 500}, status=200)

    comment = client.update_comment(comment_id=77, task_id=500, content="Reworded")

    import json

    assert responses.calls[-1].request.method == "PUT"
    assert json.loads(responses.calls[-1].request.body) == {
        "id": 77,
        "taskId": 500,
        "contentComment": "Reworded",
        "attachIsPending": False,
    }
    assert comment == {"id": 77, "taskId": 500}


# The three statuses a real space answers `workflowStatusesAvail` with. Names are Jaga's own,
# kept verbatim (CONTRIBUTING: "text we quote from Jaga"). RUF001 objects to the one-letter
# Cyrillic preposition below, which it reads as a Latin "B" — but Jaga's name is what it is.
IN_PROGRESS_NAME = "В работе"  # noqa: RUF001
SPACE_STATUSES_PAYLOAD: list[dict[str, Any]] = [
    {"id": 107391, "name": "Сделать", "categoryNameM": "status.category.todo"},
    {"id": 107389, "name": IN_PROGRESS_NAME, "categoryNameM": "status.category.inprogress"},
    {"id": 107390, "name": "Готово", "categoryNameM": "status.category.done"},
]


@responses.activate
def test_get_space_statuses(client: JagaClient) -> None:
    """The statuses of ONE space, from `workflowStatusesAvail?projectId=` — the only endpoint
    that answers with a usable list. `/v1/taskStatusRef` returns all ~90k of them."""
    _mock_login()
    responses.add(
        responses.GET, f"{API}/v1/workflowStatusesAvail", json=SPACE_STATUSES_PAYLOAD, status=200
    )

    statuses = client.get_space_statuses(11361)

    assert [(s.id, s.name, s.category) for s in statuses] == [
        (107391, "Сделать", "status.category.todo"),
        (107389, IN_PROGRESS_NAME, "status.category.inprogress"),
        (107390, "Готово", "status.category.done"),
    ]
    request = responses.calls[-1].request
    assert "projectId=11361" in str(request.url)


@responses.activate
def test_get_space_statuses_is_cached_per_space(client: JagaClient) -> None:
    """Every resolve and every regression asks for these. Cache them — but per space: two
    spaces must not see each other's statuses, or the sync would move a task into a status
    from another workflow."""
    _mock_login()
    responses.add(
        responses.GET, f"{API}/v1/workflowStatusesAvail", json=SPACE_STATUSES_PAYLOAD, status=200
    )
    responses.add(
        responses.GET,
        f"{API}/v1/workflowStatusesAvail",
        json=[{"id": 9, "name": "Other", "categoryNameM": "status.category.done"}],
        status=200,
    )

    first = client.get_space_statuses(11361)
    again = client.get_space_statuses(11361)
    other = client.get_space_statuses(22222)

    assert [s.id for s in first] == [s.id for s in again] == [107391, 107389, 107390]
    assert [s.id for s in other] == [9]
    status_calls = [c for c in responses.calls if "workflowStatusesAvail" in c.request.url]
    assert len(status_calls) == 2  # one per space, the repeat came from the cache


@responses.activate
def test_transition_task(client: JagaClient) -> None:
    """`formFields` is declared required by the API and an empty list is accepted — verified
    against a live instance. Sending it is not optional: leaving the key out is a 4xx."""
    _mock_login()
    responses.add(responses.POST, f"{API}/v1/task/updateTaskStatusAndFields", json={}, status=202)

    client.transition_task(task_id=1703944, target_status_id=107390)

    sent = json.loads(responses.calls[-1].request.body)
    assert sent == {"taskId": 1703944, "targetStatusId": 107390, "formFields": []}


@responses.activate
def test_relogins_once_on_401(client: JagaClient) -> None:
    _mock_login()
    responses.add(responses.GET, f"{API}/v1/project/list/my", json={"message": "no"}, status=401)
    responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
    responses.add(
        responses.GET,
        f"{API}/v1/project/list/my",
        json={"content": [], "totalPages": 0, "pageNumber": 0, "totalElements": 0},
        status=200,
    )
    assert client.get_projects() == []


@responses.activate
def test_second_401_in_a_row_raises_instead_of_looping(client: JagaClient) -> None:
    """The re-login happens exactly once: a second 401 is raised rather than looped on."""
    _mock_login()
    responses.add(responses.GET, f"{API}/v1/project/list/my", json={"message": "no"}, status=401)
    responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
    responses.add(responses.GET, f"{API}/v1/project/list/my", json={"message": "no"}, status=401)

    with pytest.raises(JagaAuthError):
        client.get_projects()

    logins = [call for call in responses.calls if call.request.url.endswith("/v1/auth/login")]
    assert len(logins) == 2  # the initial login plus exactly one re-login


@responses.activate
def test_403_does_not_trigger_relogin(client: JagaClient) -> None:
    """A 403 means "the token is valid, the rights are not": re-logging in would not fix it,
    only mask it.

    The error must reach the caller on the first attempt, with no second login and no repeat
    request.
    """
    _mock_login()
    responses.add(
        responses.GET, f"{API}/v1/project/list/my", json={"message": "forbidden"}, status=403
    )

    with pytest.raises(JagaAuthError) as exc_info:
        client.get_projects()

    assert exc_info.value.status_code == 403
    logins = [c for c in responses.calls if c.request.url.endswith("/v1/auth/login")]
    list_calls = [c for c in responses.calls if "/v1/project/list/my" in c.request.url]
    assert len(logins) == 1  # only the initial login, no re-login
    assert len(list_calls) == 1  # and the request was not repeated


@responses.activate
def test_raises_not_found(client: JagaClient) -> None:
    _mock_login()
    responses.add(
        responses.GET,
        f"{API}/v1/task/findExtendedWithFlexField/code/PLT-999",
        json={"message": "Task not found"},
        status=404,
    )
    with pytest.raises(JagaNotFoundError):
        client.get_task_by_code("PLT-999")


@responses.activate
def test_raises_auth_error_when_login_fails(client: JagaClient) -> None:
    responses.add(
        responses.POST, f"{API}/v1/auth/login", json={"message": "Invalid password"}, status=401
    )
    with pytest.raises(JagaAuthError):
        client.login()


@responses.activate
def test_raises_server_error(client: JagaClient) -> None:
    _mock_login()
    responses.add(responses.GET, f"{API}/v1/project/list/my", json={}, status=500)
    with pytest.raises(JagaServerError):
        client.get_projects()


# --- response body parsing ------------------------------------------------
# Jaga does not answer with JSON on every endpoint: there can be an empty body (a 204 on a
# comment) and plain text from a proxy in front of Jaga. We check this on `_send` — it is
# the only place that returns the parsed body as is; the public methods already interpret it.


@responses.activate
def test_send_returns_none_on_empty_ok_body(client: JagaClient) -> None:
    responses.add(responses.POST, f"{API}/v1/comment", body="", status=204)
    assert client._send("POST", "/v1/comment") is None


@responses.activate
def test_send_returns_text_on_non_json_ok_body(client: JagaClient) -> None:
    responses.add(
        responses.POST, f"{API}/v1/comment", body="OK", status=200, content_type="text/plain"
    )
    assert client._send("POST", "/v1/comment") == "OK"


@responses.activate
def test_send_raises_with_text_body_on_non_json_error(client: JagaClient) -> None:
    """A proxy may answer with an HTML error: the exception is still raised, with the text in
    `body`."""
    responses.add(
        responses.GET,
        f"{API}/v1/project/list/my",
        body="<html>502 Bad Gateway</html>",
        status=502,
        content_type="text/html",
    )
    with pytest.raises(JagaServerError) as exc_info:
        client._send("GET", "/v1/project/list/my")

    assert exc_info.value.status_code == 502
    assert exc_info.value.body == "<html>502 Bad Gateway</html>"


@responses.activate
def test_get_space_users_stops_and_says_so_when_a_space_has_absurdly_many_members(
    client: JagaClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A space that claims more pages than the cap must not hang the form render for minutes —
    and must not pretend it listed everyone either. Silent truncation reads as "that is all of
    them", which is exactly the kind of quiet lie this endpoint replaced."""
    _mock_login()
    for page in range(MAX_MEMBER_PAGES):
        responses.add(
            responses.GET,
            _matrix_url(1),
            json=_matrix_page(
                [
                    {
                        "id": page,
                        "displayName": f"Member {page}",
                        "email": f"m{page}@e.com",
                        "isGroup": False,
                        "type": "USER",
                    }
                ],
                total_pages=MAX_MEMBER_PAGES + 5,
            ),
            status=200,
        )

    with caplog.at_level(logging.WARNING, logger="sentry_jaga.client"):
        users = client.get_space_users(1)

    assert len(users) == MAX_MEMBER_PAGES
    pages_read = [c for c in responses.calls if "userRoles" in c.request.url]
    assert len(pages_read) == MAX_MEMBER_PAGES, "the cap must actually stop the loop"
    assert "jaga.client.member_list_truncated" in caplog.text


@responses.activate
def test_find_person_by_email_returns_none_when_jaga_answers_without_a_uuid(
    client: JagaClient,
) -> None:
    """A 200 with no `uuid` in it is not a person we can assign: the UUID is the only thing the
    attribute takes. Returning a `Person` with an empty uuid would put `""` on a real task."""
    _mock_login()
    responses.add(
        responses.POST,
        f"{API}/v1/team/userProfile/findByMailOrName",
        json={"coreId": 1, "mail": "a@e.com"},
        status=200,
    )

    assert client.find_person_by_email("a@e.com") is None


@responses.activate
def test_get_space_users_is_cached(client: JagaClient) -> None:
    """The create form's space and type selects both carry `updatesForm`, so Sentry re-renders the
    whole form — and re-runs this — every time either is touched. Uncached, each of those renders
    would walk every page of the member list again."""
    _mock_login()
    responses.add(
        responses.GET,
        _matrix_url(1),
        json=_matrix_page(
            [
                {
                    "id": 1,
                    "displayName": "One",
                    "email": "one@e.com",
                    "isGroup": False,
                    "type": "USER",
                }
            ]
        ),
        status=200,
    )

    first = client.get_space_users(1)
    second = client.get_space_users(1)

    assert first == second == [("one@e.com", "One")]
    matrix_calls = [c for c in responses.calls if "userRoles" in c.request.url]
    assert len(matrix_calls) == 1, "the second render must come from the cache"


@responses.activate
def test_get_space_users_falls_back_when_the_matrix_is_not_even_a_page(client: JagaClient) -> None:
    """A 200 whose body is not a page is an answer we cannot read. It must reach the fallback —
    not escape as an `AttributeError` from `.get` and take the whole create form down with it.
    This API has already returned shapes its own spec did not promise, five times over."""
    _mock_login()
    responses.add(responses.GET, _matrix_url(1), json=["not", "a", "page"], status=200)
    responses.add(
        responses.GET,
        f"{API}/v1/project/getUserProfileDtos/1",
        json=[
            {
                "id": 1,
                "personUuid": "u",
                "email": "fallback@example.com",
                "displayName": "Fallback",
                "canBeAssign": True,
                "isBlocked": False,
            }
        ],
        status=200,
    )

    assert client.get_space_users(1) == [("fallback@example.com", "Fallback")]
