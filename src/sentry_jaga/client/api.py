"""HTTP client for the Jaga API."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from urllib.parse import urljoin

import requests

from sentry_jaga.client.auth import Cache, InMemoryCache, TokenManager
from sentry_jaga.client.exceptions import JagaApiError, JagaError, error_from_response
from sentry_jaga.client.models import Attribute, Person, Project, Status, TaskRef, TaskType, Token
from sentry_jaga.fields import ASSIGNEE_OBJECT_TYPE, extract_title

logger = logging.getLogger("sentry_jaga.client")

API_PREFIX = "/external-api"
STATUS_MODIFIER_TODO = 1
DEFAULT_TIMEOUT = 30
DEFAULT_PAGE_SIZE = 100
# `applicationMnemo` is a required path segment the spec never gives a value for ("Мнемоника
# приложения", no enum, no example). "JAGA" is the value the live instance accepts.
APPLICATION_MNEMO = "JAGA"
# Cap on the member-list walk (2000 people), so a pathological space cannot hang a form render.
MAX_MEMBER_PAGES = 20
# The link form re-fetches the spaces on every keystroke (`updatesForm`, no debounce); a short
# TTL absorbs the burst.
PROJECTS_CACHE_TTL = 60
# Statuses only change when someone edits the workflow behind a space.
STATUSES_CACHE_TTL = 300
# A person's UUID is immutable. Misses are cached too, so a Sentry user with no Jaga account does
# not cost a round trip on every assignment.
PERSON_CACHE_TTL = 3600
# Same reasoning as PROJECTS_CACHE_TTL: the `updatesForm` cascade re-renders the form repeatedly.
MEMBERS_CACHE_TTL = 60


# Jaga reports "no such user" as a 400 with a `NotFoundException` inside it, so the status code
# alone cannot be trusted. These markers are matched against the whole body; any other 400 is a
# bug on our side and must propagate rather than be cached as "no such person".
NOT_FOUND_MARKERS = ("notfoundexception", "does not exist")


def _looks_like_not_found(exc: JagaApiError) -> bool:
    """Does this error really mean "no such object", or is it a 400 we do not understand?

    A 404 is taken at its word. A 400 has to prove itself from the body: on this API a 400 is also
    the generic validation error, and a 404 has meant "Spring has no handler for that route".
    """
    if exc.status_code == 404:
        return True
    if exc.status_code != 400:
        return False
    haystack = f"{exc} {json.dumps(exc.body, ensure_ascii=False)}".lower()
    return any(marker in haystack for marker in NOT_FOUND_MARKERS)


class JagaClient:
    """Client for the external Jaga API. Does not depend on Sentry."""

    def __init__(
        self,
        instance_url: str,
        email: str,
        password: str,
        *,
        cache: Cache | None = None,
        session: requests.Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = instance_url.rstrip("/") + API_PREFIX
        self._email = email
        self._password = password
        self._timeout = timeout
        self._session = session or requests.Session()
        self._cache = cache or InMemoryCache()
        prefix = self._cache_prefix(instance_url, email)
        self._projects_cache_key = f"{prefix}:projects"
        self._statuses_cache_prefix = f"{prefix}:statuses"
        self._person_cache_prefix = f"{prefix}:person"
        self._members_cache_prefix = f"{prefix}:members"
        self._tokens = TokenManager(
            login=self.login,
            refresh=self.refresh,
            cache=self._cache,
            cache_key=f"{prefix}:token",
        )

    @staticmethod
    def _cache_prefix(instance_url: str, email: str) -> str:
        """Shared cache key prefix for one instance + service account pair."""
        digest = hashlib.sha256(f"{instance_url}|{email}".encode()).hexdigest()[:32]
        return f"sentry-jaga:{digest}"

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._session.request(method, self._url(path), timeout=self._timeout, **kwargs)
        if response.ok:
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return response.text
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        raise error_from_response(response.status_code, body)

    def _authed(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send a request with a Bearer token; on a 401, re-login exactly once.

        Only on a 401: a Jaga 403 means the token is valid but lacks rights to the object, and
        re-logging in would only mask that.
        """
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._tokens.get_access_token()}"
        try:
            return self._send(method, path, headers=headers, **kwargs)
        except JagaApiError as exc:
            if exc.status_code != 401:
                raise
            self._tokens.invalidate()
            headers["Authorization"] = f"Bearer {self._tokens.get_access_token()}"
            return self._send(method, path, headers=headers, **kwargs)

    # --- authentication -------------------------------------------------

    def login(self) -> Token:
        payload = self._send(
            "POST", "/v1/auth/login", json={"email": self._email, "password": self._password}
        )
        return Token.from_api(payload)

    def refresh(self, refresh_token: str) -> Token:
        payload = self._send("POST", "/v1/auth/refresh", json={"refreshToken": refresh_token})
        return Token.from_api(payload)

    # --- reference data -------------------------------------------------

    def get_projects(self) -> list[Project]:
        """Every available space (across all pages), with a short-lived cache.

        The cache is shared with the token (in production, Sentry's Django cache), so it outlives
        a single HTTP request — the link form fetches this list on every keystroke.
        """
        cached = self._cache.get(self._projects_cache_key)
        if cached is not None:
            return [Project.from_api(item) for item in cached.get("content", [])]

        content = self._fetch_all_projects()
        self._cache.set(self._projects_cache_key, {"content": content}, timeout=PROJECTS_CACHE_TTL)
        return [Project.from_api(item) for item in content]

    def _fetch_all_projects(self) -> list[dict[str, Any]]:
        """Read every page of `/v1/project/list/my` — the 101st space must be visible too."""
        items: list[dict[str, Any]] = []
        page = 0
        while True:
            payload = self._authed(
                "GET", "/v1/project/list/my", params={"page": page, "size": DEFAULT_PAGE_SIZE}
            )
            items.extend(payload.get("content", []))
            page += 1
            if page >= int(payload.get("totalPages") or 0):
                return items

    def get_task_types(self, project_id: int) -> list[TaskType]:
        payload = self._authed("GET", f"/v1/project/{project_id}/taskType")
        items = payload if isinstance(payload, list) else [payload]
        return [TaskType.from_api(item) for item in items]

    def get_task_type_attributes(self, project_id: int, task_type_id: int) -> list[Attribute]:
        payload = self._authed("GET", f"/v1/project/{project_id}/taskType/{task_type_id}")
        attributes: list[Attribute] = []
        for group in payload.get("groups", []):
            for raw in group.get("attributes", []):
                attributes.append(Attribute.from_api(raw))
        return attributes

    def get_dictionary_values(self, dictionary_id: int) -> list[tuple[str, str]]:
        payload = self._authed("GET", f"/v1/listRef/{dictionary_id}/any")
        items = sorted(payload.get("items", []), key=lambda item: item.get("orderNum", 0))
        return [(str(item["id"]), item["value"]) for item in items]

    def get_space_users(self, space_id: int) -> list[tuple[str, str]]:
        """Members of a space a task can be assigned to: (email, displayName) pairs.

        The value is an EMAIL, not the person UUID that `task.assignee_uuid` finally takes: the
        member list carries no UUID, and `find_person_by_email` resolves only ONE per call, so
        UUIDs here would cost N HTTP calls before the form can be drawn. The pick is resolved at
        submit time instead — see `issue_config.resolve_assignee_cells`. Members with no email
        are dropped; they could not be resolved later.
        """
        cache_key = f"{self._members_cache_prefix}:{space_id}"
        cached = self._cache.get(cache_key)
        if cached is None:
            cached = {"members": self._space_members(space_id)}
            self._cache.set(cache_key, cached, timeout=MEMBERS_CACHE_TTL)

        return [
            (str(m["email"]), str(m.get("displayName") or m["email"]))
            for m in cached.get("members", [])
            if m.get("email")
        ]

    def _space_members(self, space_id: int) -> list[dict[str, Any]]:
        """The people in a space — from the user-role matrix, not from the documented endpoint.

        The documented `GET /v1/project/getUserProfileDtos/{space}` answers `200 []` for EVERY
        space on the live instance, so it is dead: it silently reports that nobody is there. The
        role matrix answers properly and is tried first (see `APPLICATION_MNEMO`).

        `getUserProfileDtos` is kept only as a fallback for when the matrix ERRORS (an instance
        whose application is named something else). An EMPTY matrix is taken at face value — a
        space really can have no members, and falling back would just hide the dead endpoint again.
        """
        try:
            return self._fetch_matrix_members(space_id)
        except JagaApiError:
            logger.warning(
                "jaga.client.role_matrix_unavailable",
                extra={"space_id": space_id, "application_mnemo": APPLICATION_MNEMO},
                exc_info=True,
            )

        payload = self._authed("GET", f"/v1/project/getUserProfileDtos/{space_id}")
        items = payload if isinstance(payload, list) else []
        return [
            item
            for item in items
            if item.get("canBeAssign") and not item.get("isBlocked") and item.get("email")
        ]

    def _fetch_matrix_members(self, space_id: int) -> list[dict[str, Any]]:
        """Every page of the user-role matrix, flattened to the people in it.

        The matrix pages over ROLES, each carrying its users, so one person appears once per role
        they hold — hence the dedupe. Groups are dropped: a group cannot be a task's executor.
        A member's plain `id` here is the TEAM id, not the Core id (see `Person`); nothing reads
        it, the email is what travels.
        """
        by_email: dict[str, dict[str, Any]] = {}
        page = 0
        while page < MAX_MEMBER_PAGES:
            payload = self._authed(
                "GET",
                f"/v1/team/userRoles/applications/{APPLICATION_MNEMO}/projects/{space_id}",
                params={"page": page, "size": DEFAULT_PAGE_SIZE},
            )
            # Raise a `JagaApiError` rather than let `.get` blow up with an `AttributeError`:
            # only the former is caught by `_space_members`, which then falls back. An
            # AttributeError would sail past its `except` and take the create form down.
            if not isinstance(payload, dict):
                raise JagaApiError(
                    200, f"the member list of space {space_id} is not a page", body=payload
                )
            for row in payload.get("content", []):
                for entry in row.get("usersRoles", []):
                    user = entry.get("user") or {}
                    email = user.get("email")
                    if email and not user.get("isGroup") and user.get("type") == "USER":
                        by_email.setdefault(str(email), user)

            page += 1
            if page >= int(payload.get("totalPages") or 0):
                break
        else:
            logger.warning(
                "jaga.client.member_list_truncated",
                extra={"space_id": space_id, "pages_read": MAX_MEMBER_PAGES},
            )
        return list(by_email.values())

    def find_person_by_email(self, email: str) -> Person | None:
        """The one door to a person's UUID. Returns None only when Jaga says it knows no such
        email, and raises on everything else.

        Jaga answers an unknown email with HTTP **400**, not 404, burying a `NotFoundException`
        ("User with email ... does not exists") in the body. So the body has to be read: treating
        every 400 as "nobody" would swallow our own bugs (a renamed field, a moved route, a WAF)
        and cache them for an hour as "that person does not exist". `_looks_like_not_found` tells
        the two apart; an unrecognised 400 propagates. Hits and misses are both cached.
        """
        address = email.strip()
        if not address:
            return None

        key = f"{self._person_cache_prefix}:{hashlib.sha256(address.lower().encode()).hexdigest()}"
        cached = self._cache.get(key)
        if cached is not None:
            return Person.from_api(cached) if cached else None

        try:
            payload = self._authed(
                "POST", "/v1/team/userProfile/findByMailOrName", json={"searchText": address}
            )
        except JagaApiError as exc:
            if not _looks_like_not_found(exc):
                raise
            self._cache.set(key, {}, timeout=PERSON_CACHE_TTL)
            return None

        if not isinstance(payload, dict) or not payload.get("uuid"):
            return None

        # The endpoint is find-by-mail-OR-NAME and promises no exact match, so a fuzzy hit could
        # hand back a different human — who would then be assigned a real task. A mismatch is
        # treated as "not found".
        found = Person.from_api(payload)
        if found.email.lower() != address.lower():
            logger.warning(
                "jaga.client.person_email_mismatch",
                extra={"returned": found.email, "asked_for": address},
            )
            return None

        self._cache.set(key, payload, timeout=PERSON_CACHE_TTL)
        return found

    def set_task_assignees(self, task_id: int, field_id: int, person_uuids: list[str]) -> None:
        """Set (or clear) the people a task is assigned to.

        This is an attribute write, not a role change: the spec's `PUT /v1/taskRole/task/{id}/
        executor` does not exist on the instance (404, `No static resource ...`). The assignee is
        the ordinary EAV attribute `task.assignee_uuid`, written through `PATCH /v1/task/{taskId}`.

        Verified live: the value travels as a LIST (the attribute is `multiple`) and writing it
        also fills the task's top-level `executors`. An EMPTY list CLEARS the assignee, which is
        what an unassignment in Sentry has to do.
        """
        self._authed(
            "PATCH",
            f"/v1/task/{task_id}",
            json={
                "fieldId": field_id,
                "value": person_uuids,
                "referenceValue": True,
                "addInfo": {},
                "objectTypeNameM": ASSIGNEE_OBJECT_TYPE,
            },
        )

    def get_space_statuses(self, space_id: int) -> list[Status]:
        """The statuses reachable inside one space, with a short-lived cache.

        Not `/v1/taskStatusRef`: that returns every status the instance knows — ~90k of them over
        ~15k workflows, since each workflow owns copies of the same handful. Scoped to a space it
        is a list of three or four. (This is also why status mapping keys on the category; see
        `Status`.)
        """
        key = f"{self._statuses_cache_prefix}:{space_id}"
        cached = self._cache.get(key)
        if cached is not None:
            return [Status.from_api(item) for item in cached.get("items", [])]

        payload = self._authed("GET", "/v1/workflowStatusesAvail", params={"projectId": space_id})
        items: list[dict[str, Any]] = payload if isinstance(payload, list) else []
        self._cache.set(key, {"items": items}, timeout=STATUSES_CACHE_TTL)
        return [Status.from_api(item) for item in items]

    def get_labels(self) -> list[tuple[str, str]]:
        """Every label, as (id, name) pairs — the value of `task.label_id` is the label id.

        Labels have no dictionary of their own (`/v1/listRef` knows nothing of them); this POST is
        the only listing endpoint. An empty `searchText` means "no filter".
        """
        payload = self._authed(
            "POST",
            "/v1/labels/getPage",
            json={"searchText": "", "order": "ASC", "orderBy": "name"},
        )
        return [
            (str(item["id"]), str(item.get("name") or item["id"]))
            for item in payload.get("content", [])
        ]

    def get_or_create_label(self, name: str) -> int:
        """The id of the label with this name — created on the spot if Jaga has no such label.

        `/v1/labels/list` is a get-or-create despite its name: it returns the labels named in the
        body and creates the missing ones. Verified live — called twice with the same name it
        returns the same id. It is needed because the auto-label must already be an id when the
        task is created, and `/v1/labels/getPage` (see `get_labels`) only lists what exists.
        """
        payload = self._authed("POST", "/v1/labels/list", json={"names": [name]})
        labels = payload.get("labels") or [] if isinstance(payload, dict) else []
        if not labels:
            # A get-or-create that returns nothing neither found nor made the label; fail here
            # rather than on an IndexError.
            raise JagaError(f"Jaga returned no label for {name!r}.")
        return int(labels[0]["id"])

    # --- tasks ----------------------------------------------------------

    def create_task(
        self, project_id: int, task_type_id: int, attributes: list[dict[str, Any]]
    ) -> TaskRef:
        payload = self._authed(
            "POST",
            f"/v1/task/createByTaskType/{project_id}/{task_type_id}",
            json={
                "orderNum": 0,
                "statusModifier": STATUS_MODIFIER_TODO,
                "attachmentIds": [],
                "attributes": attributes,
            },
        )
        return TaskRef(id=payload["id"], code=payload["code"], title="")

    def get_task_by_code(self, code: str) -> dict[str, Any]:
        result = self._authed("GET", f"/v1/task/findExtendedWithFlexField/code/{code}")
        return dict(result)

    def search_tasks_globally(self, text: str, *, size: int = 20) -> list[TaskRef]:
        """Search tasks across every space the service account can see.

        `/v1/task/searchByTitleCode` cannot do this — it demands a `projectId` and searches one
        space. Two facts confirmed live:

        * the spec declares the response `array<TaskPageApiDto>`; the wire returns a single page
          OBJECT. The spec is wrong.
        * a result's title lives in its EAV `attributes`, and `projectId`/`projectCode`/
          `projectTitle` come back null — the space is simply not in this answer.

        The body is the filter; an empty list means "no filter" and the whole query is the `query`
        parameter.
        """
        payload = self._authed(
            "POST",
            "/v1/globalSearch/findTaskList",
            params={"query": text, "page": 0, "size": size},
            json=[],
        )
        content = payload.get("content", []) if isinstance(payload, dict) else []
        return [
            TaskRef(id=int(raw["id"]), code=str(raw["code"]), title=extract_title(raw))
            for raw in content
        ]

    def attach_file(
        self, space_id: int, task_id: int, filename: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        """Upload a file and attach it to a task. Returns the attachment Jaga created.

        `/v1/attacher/file/create` is summarised as "upload attachment without binding to entity"
        yet takes a `taskId` anyway — verified live, the file lands ON the task. `projectId` is a
        mandatory query param (Jaga files every attachment under a space), which is why the space
        must be passed in even though the task already knows it.

        `requests` builds the multipart body and its boundary from `files=`, so we must not set a
        Content-Type header of our own.
        """
        payload = self._authed(
            "POST",
            "/v1/attacher/file/create",
            params={"projectId": space_id, "taskId": task_id},
            files={"file": (filename, content, content_type)},
        )
        return dict(payload) if isinstance(payload, dict) else {}

    def transition_task(self, task_id: int, target_status_id: int) -> None:
        """Move a task to another status.

        `formFields` is declared required, and an empty list IS accepted — verified live in both
        directions ("Сделать" -> "Готово" -> "Сделать"). A transition whose workflow demands a
        filled form is refused; `issue_config.apply_status_sync` falls back to a comment on any
        Jaga error.
        """
        self._authed(
            "POST",
            "/v1/task/updateTaskStatusAndFields",
            json={"taskId": task_id, "targetStatusId": target_status_id, "formFields": []},
        )

    def create_comment(self, task_id: int, content: str) -> dict[str, Any]:
        """Post a comment and return it as Jaga created it — `id` included.

        Sentry keeps that `id` on the note and hands it back to `update_comment` when the note is
        edited (see `issue_config.post_task_comment`).
        """
        payload = self._authed(
            "POST",
            "/v1/comment",
            json={"taskId": task_id, "contentComment": content, "attachIsPending": False},
        )
        return dict(payload) if isinstance(payload, dict) else {}

    def update_comment(self, comment_id: int, task_id: int, content: str) -> dict[str, Any]:
        """Rewrite an existing comment.

        `taskId` travels with the id because `CommentApiDto` declares it required on the update
        as well as on the create — the endpoint takes the same schema both ways.
        """
        payload = self._authed(
            "PUT",
            "/v1/comment",
            json={
                "id": comment_id,
                "taskId": task_id,
                "contentComment": content,
                "attachIsPending": False,
            },
        )
        return dict(payload) if isinstance(payload, dict) else {}
