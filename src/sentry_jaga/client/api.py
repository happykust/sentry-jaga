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
# The application the space-membership endpoints are scoped to. `applicationMnemo` is a required
# path segment that the spec describes only as "Мнемоника приложения", with no enum, no example
# and no endpoint that lists the applications. "JAGA" is the value the live instance accepts.
APPLICATION_MNEMO = "JAGA"
# A space with more members than this reads its first 20 pages (2000 people) and logs that it
# stopped. The cap exists so that a pathological space cannot hang a form render for minutes;
# it is far above any space a human would pick an assignee from in a dropdown.
MAX_MEMBER_PAGES = 20
# The list of spaces rarely changes, yet the link form re-fetches it on every keystroke
# (`updatesForm`, with no debounce). A short TTL absorbs the burst without risking a
# stale list for long.
PROJECTS_CACHE_TTL = 60
# The statuses of a space change only when someone edits the workflow behind it — rare, and
# never mid-incident. Every resolve and every regression asks for them, so cache them; five
# minutes is short enough that a workflow edit lands on its own without a restart.
STATUSES_CACHE_TTL = 300
# A person's UUID is immutable — it is the identity itself, not a property of it. An hour is
# simply a bound on remembering someone who has been deleted from Jaga, and on the negative
# entries: a Sentry user with no Jaga account must not cost a round trip on every assignment.
PERSON_CACHE_TTL = 3600
# The members of a space change when someone joins or leaves it — rare. Sixty seconds, like the
# list of spaces: long enough to absorb the burst of re-renders the `updatesForm` cascade causes,
# short enough that a new colleague appears in the dropdown without anyone restarting anything.
MEMBERS_CACHE_TTL = 60


# Jaga reports "no such user" as a 400 with a `NotFoundException` inside it, so the status code
# alone cannot be trusted to mean it. These are the markers of a genuine not-found, matched against
# the whole body: the exception class name Jaga names, and the message it writes. Anything else
# with a 400 is a bug on our side or a broken deployment, and has to be heard, not cached away.
NOT_FOUND_MARKERS = ("notfoundexception", "does not exist")


def _looks_like_not_found(exc: JagaApiError) -> bool:
    """Does this error actually say "no such object" — or is it merely a 400 we do not understand?

    A 404 is taken at its word. A 400 has to prove itself: on this very API a 404 has already
    turned out to mean "Spring has no handler for that route" (`/v1/taskRole/.../executor`), and a
    400 is also the generic validation error. Reading the body is the only way to tell a person
    Jaga has never heard of from a request Jaga could not parse.
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
        """Members of a space a task can be assigned to: (email, displayName) pairs.

        The value is an EMAIL, not the person UUID the `task.assignee_uuid` attribute ultimately
        takes. That is deliberate, and it is the whole reason the assignee select works at all:

        * the member list gives no UUID. Only three endpoints in the whole API return one, and
          two of them are unusable here (see `_space_members`), leaving `find_person_by_email`
          — which resolves ONE person per call.
        * so filling a select of N members with UUIDs would cost N HTTP calls before the create
          form can be drawn. A space with fifty people would hang the form for seconds, every
          time it is opened.

        Carrying the email instead costs nothing to list, and the UUID is resolved for the one
        person who was actually picked, at submit time — one call instead of N. See
        `issue_config.resolve_assignee_cells`.

        Members with no email are dropped: they cannot be resolved later, so offering them would
        only build a form whose submit fails.

        Cached, like the spaces and the statuses are, and for the same reason: the create form's
        space and type selects both carry `updatesForm`, so Sentry re-renders the whole form —
        and re-runs this — every time either is touched. Uncached, that would be a fresh walk of
        every page of the member list on each of those renders.
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

        `GET /v1/project/getUserProfileDtos/{space}` is what the API documents for this, and it
        is what this client used to call. Against the live instance it answers 200 with an empty
        list for EVERY space — including one whose owner is the very account asking. It does not
        fail; it silently reports that nobody is there, and the assignee select rendered empty
        with no error anywhere. That is the bug this replaced.

        The matrix answers properly. `applicationMnemo` is a required path segment that the spec
        documents as "Мнемоника приложения" and never gives a single value for; "JAGA" is the one
        the instance accepts.

        It is still tried the documented way first — but only as a fallback, when the matrix
        itself errors (an instance that names its application something else). An EMPTY matrix is
        taken at face value: a space really can have no members, and quietly falling back on a
        second empty answer would only hide it again.
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

        The matrix is a page of ROLES, each carrying the users that hold it, so one person shows
        up once per role they have — hence the dedupe. Groups are dropped: a group cannot be the
        executor of a task, and `find_person_by_email` would never resolve one.

        Every member here carries `id`, and that id is the TEAM id, not the Core id (see
        `Person`). Nothing reads it: the email is what travels.
        """
        by_email: dict[str, dict[str, Any]] = {}
        page = 0
        while page < MAX_MEMBER_PAGES:
            payload = self._authed(
                "GET",
                f"/v1/team/userRoles/applications/{APPLICATION_MNEMO}/projects/{space_id}",
                params={"page": page, "size": DEFAULT_PAGE_SIZE},
            )
            # A page is a dict, and an answer that is not one is an answer we cannot read. Raising
            # a `JagaApiError` — rather than letting `.get` blow up with an `AttributeError` — is
            # what lets `_space_members` catch it and fall back; an AttributeError would sail past
            # its `except` and take the whole create form down with it. This API has already
            # returned shapes its own spec did not promise, five times over.
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
        """The one door to a person's UUID. Returns None only when Jaga *says* it knows no such
        email — and raises on everything else.

        Jaga answers an unknown email with HTTP **400**, not 404, and buries a `NotFoundException`
        with "User with email ... does not exists" in the body. So a 400 here has to be read, not
        assumed: a blanket "400 means nobody" would swallow OUR OWN bugs — a renamed field, a
        moved route, a WAF in front of the API — and turn them into a confident, hour-long cached
        "that person does not exist", about people who are standing right there in the dropdown.
        That is the exact failure this whole change was written to kill (`_space_members`), and it
        must not be reintroduced one layer down. `_looks_like_not_found` is what tells the two
        apart, and an unrecognised 400 propagates.

        A person's UUID never changes, so the answer is cached for an hour — the miss too, so that
        a Sentry user with no Jaga account does not cost a round trip on every assignment.
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

        # The endpoint is find-by-mail-OR-NAME, and we asked it by mail. Nothing in its contract
        # promises an exact match, and a fuzzy one would hand back a different human — whom we
        # would then assign a real task to, silently and with total confidence. A mismatch is
        # treated as "not found", which is the safe reading of an answer we did not ask for.
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

        This is an attribute write, not a role change. `PUT /v1/taskRole/task/{id}/executor` is
        what the spec offers for this and it DOES NOT EXIST on the instance — it 404s with
        `No static resource taskAssignee/task/.../executor`, i.e. Spring has no handler for that
        route at all. The assignee is the ordinary EAV attribute `task.assignee_uuid`, and
        `PATCH /v1/task/{taskId}` takes exactly the cell the create already builds.

        Verified against a live instance: the value travels as a LIST (the attribute is
        `multiple`), and writing it fills the task's top-level `executors` too — the attribute IS
        what Jaga's UI calls the executor. An empty list clears the field, which is what an
        unassignment in Sentry has to do.
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

    def search_tasks_globally(self, text: str, *, size: int = 20) -> list[TaskRef]:
        """Search tasks across every space the service account can see.

        This is what `/v1/task/searchByTitleCode` cannot do: that one demands a `projectId` and
        searches inside one space, which is why linking used to make the user pick a space first.

        Two things about this endpoint are worth knowing, both confirmed against a live instance:

        * the spec declares the response as `array<TaskPageApiDto>`; what comes back is a single
          page OBJECT. The spec is wrong — this follows the wire.
        * a task in the results carries its title inside `attributes` (Jaga's EAV, same as
          anywhere else), and its `projectId`/`projectCode`/`projectTitle` come back **null** —
          so the space a task lives in simply is not in this answer. Hence `TaskRef.title` is
          read out of the attributes, and the caller shows the code and the title, nothing more.

        The body is the filter, and an empty list means "no filter" — the whole query is the
        `query` parameter.
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
