"""HTTP client for the Jaga API."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urljoin

import requests

from sentry_jaga.client.auth import Cache, InMemoryCache, TokenManager
from sentry_jaga.client.exceptions import JagaApiError, JagaError, error_from_response
from sentry_jaga.client.models import Attribute, Project, Status, TaskRef, TaskType, Token

API_PREFIX = "/external-api"
STATUS_MODIFIER_TODO = 1
DEFAULT_TIMEOUT = 30
DEFAULT_PAGE_SIZE = 100
# The list of spaces rarely changes, yet the link form re-fetches it on every keystroke
# (`updatesForm`, with no debounce). A short TTL absorbs the burst without risking a
# stale list for long.
PROJECTS_CACHE_TTL = 60
# The statuses of a space change only when someone edits the workflow behind it — rare, and
# never mid-incident. Every resolve and every regression asks for them, so cache them; five
# minutes is short enough that a workflow edit lands on its own without a restart.
STATUSES_CACHE_TTL = 300


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

        Only on a 401: in Jaga a 403 means "the token is valid, but you have no rights to
        this object" — re-logging in will not fix that, it will only add a request and
        mask the real cause.
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

        The cache is shared with the token (in production, Sentry's Django cache), so it
        outlives a single HTTP request: the link form fetches this list on every keystroke.
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
        """Members of a space a task can be assigned to: (personUuid, displayName) pairs.

        The value of `task.assignee_uuid` is the person UUID — the cross-system id — and NOT
        the numeric profile `id`, so a profile without one is unusable and dropped.

        Blocked accounts and profiles Jaga marks as not assignable are dropped as well:
        offering them would only build a task Jaga refuses to accept.
        """
        payload = self._authed("GET", f"/v1/project/getUserProfileDtos/{space_id}")
        items = payload if isinstance(payload, list) else []
        users: list[tuple[str, str]] = []
        for item in items:
            person_uuid = item.get("personUuid")
            if not person_uuid or not item.get("canBeAssign") or item.get("isBlocked"):
                continue
            users.append((str(person_uuid), str(item.get("displayName") or person_uuid)))
        return users

    def get_space_statuses(self, space_id: int) -> list[Status]:
        """The statuses reachable inside one space, with a short-lived cache.

        This endpoint — and NOT `/v1/taskStatusRef` — is what makes the status sync possible.
        `/v1/taskStatusRef` answers with every status the instance knows: ~90k of them over
        ~15k workflows, because each workflow owns its own copies of the same handful of
        statuses. Scoped to a space, the same data is a list of three or four.
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

        Labels have no dictionary of their own: `/v1/listRef` knows nothing about them, and
        the only listing endpoint is this POST. Every field of the request body is optional;
        an empty `searchText` means "no filter".
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

        `/v1/labels/list` is a get-or-create, despite the name: it answers with the labels named
        in the body and makes the ones that do not exist yet. Verified against a live instance —
        called twice with the same name, it returns the same id both times.

        There is no other way round it: the auto-label has to be an id by the time the task is
        created, and a Jaga instance that has never seen this integration has no such label yet.
        `/v1/labels/getPage` (see `get_labels`) only lists what already exists.
        """
        payload = self._authed("POST", "/v1/labels/list", json={"names": [name]})
        labels = payload.get("labels") or [] if isinstance(payload, dict) else []
        if not labels:
            # The endpoint is a get-or-create: an empty answer means it neither found nor made
            # the label, and there is no id to put on the task. Say so instead of failing later
            # on an IndexError.
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

    def search_tasks(self, project_id: int, text: str, *, size: int = 20) -> list[TaskRef]:
        payload = self._authed(
            "GET",
            "/v1/task/searchByTitleCode",
            params={"projectId": project_id, "searchText": text, "page": 0, "size": size},
        )
        return [TaskRef.from_api(item) for item in payload.get("content", [])]

    def attach_file(
        self, space_id: int, task_id: int, filename: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        """Upload a file and attach it to a task. Returns the attachment Jaga created.

        The summary of `/v1/attacher/file/create` reads "upload attachment without binding to
        entity", and the endpoint takes a `taskId` anyway — verified against a live instance:
        with it, the file lands ON the task, not in limbo. `projectId` is mandatory (Jaga files
        every attachment under a space), which is why the space has to be passed down here even
        though the task already knows which one it lives in.

        `requests` builds the multipart body from `files=` — including its boundary, which is why
        no Content-Type header of ours may go with it. `_send` passes `files=` straight through.
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

        `formFields` is declared required by the API, and an empty list is accepted: verified
        against a live instance, in both directions ("Сделать" -> "Готово" -> "Сделать"). A
        transition whose workflow demands a filled form would be refused here — the caller
        (`issue_config.apply_status_sync`) falls back to a comment on any Jaga error.
        """
        self._authed(
            "POST",
            "/v1/task/updateTaskStatusAndFields",
            json={"taskId": task_id, "targetStatusId": target_status_id, "formFields": []},
        )

    def create_comment(self, task_id: int, content: str) -> dict[str, Any]:
        """Post a comment and return it as Jaga created it — `id` included.

        The `id` is what makes an edit possible later: Sentry keeps it on the note and hands it
        back to `update_comment` when the note is edited (see `issue_config.post_task_comment`).
        Jaga answers a create with the whole `CommentApiDto`; the guard is for the day it
        answers 200 with an empty body, which would otherwise blow up in the caller as a
        `TypeError` on `dict(None)`.
        """
        payload = self._authed(
            "POST",
            "/v1/comment",
            json={"taskId": task_id, "contentComment": content, "attachIsPending": False},
        )
        return dict(payload) if isinstance(payload, dict) else {}

    def update_comment(self, comment_id: int, task_id: int, content: str) -> dict[str, Any]:
        """Rewrite an existing comment.

        `taskId` travels along with the id because Jaga's `CommentApiDto` declares it required
        on the update as well as on the create — the endpoint takes the same schema both ways.
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
