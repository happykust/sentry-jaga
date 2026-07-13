import json
from typing import Any

import pytest
import responses

from sentry_jaga.client.api import JagaClient
from sentry_jaga.client.auth import InMemoryCache
from sentry_jaga.client.exceptions import JagaAuthError, JagaNotFoundError, JagaServerError

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
                        {"id": 100, "name": "Title", "objectTypeNameM": "task.title"},
                        {"id": 101, "name": "Description", "objectTypeNameM": "task.content_data"},
                    ],
                },
                {
                    "title": "Other",
                    "orderNum": 1,
                    "attributes": [
                        {
                            "id": 102,
                            "name": "Priority",
                            "objectTypeNameM": "task.priority",
                            "dictionaryId": 55,
                        }
                    ],
                },
            ],
        },
        status=200,
    )
    attrs = client.get_task_type_attributes(1, 10)
    assert [a.id for a in attrs] == [100, 101, 102]
    assert attrs[2].dictionary_id == 55


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
def test_search_tasks(client: JagaClient) -> None:
    _mock_login()
    responses.add(
        responses.GET,
        f"{API}/v1/task/searchByTitleCode",
        json={
            "content": [{"id": 5, "code": "PLT-5", "title": "Login is broken", "typeRef": {}}],
            "totalPages": 1,
            "pageNumber": 0,
            "totalElements": 1,
        },
        status=200,
    )
    results = client.search_tasks(project_id=1, text="login")
    assert [(r.code, r.title) for r in results] == [("PLT-5", "Login is broken")]


@responses.activate
def test_create_comment(client: JagaClient) -> None:
    _mock_login()
    responses.add(responses.POST, f"{API}/v1/comment", json={"id": 1, "taskId": 500}, status=200)
    client.create_comment(task_id=500, content="Resolved in Sentry")

    import json

    sent = json.loads(responses.calls[-1].request.body)
    assert sent == {
        "taskId": 500,
        "contentComment": "Resolved in Sentry",
        "attachIsPending": False,
    }


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
