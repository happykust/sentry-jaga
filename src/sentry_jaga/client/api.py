"""HTTP-клиент API Яги."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urljoin

import requests

from sentry_jaga.client.auth import Cache, InMemoryCache, TokenManager
from sentry_jaga.client.exceptions import JagaApiError, error_from_response
from sentry_jaga.client.models import Attribute, Project, TaskRef, TaskType, Token

API_PREFIX = "/external-api"
STATUS_MODIFIER_TODO = 1
DEFAULT_TIMEOUT = 30
DEFAULT_PAGE_SIZE = 100
# Список пространств меняется редко, а форма линковки перезапрашивает его на каждое
# нажатие клавиши (`updatesForm` без дебаунса). Короткий TTL гасит шквал, не рискуя
# надолго показать устаревший список.
PROJECTS_CACHE_TTL = 60


class JagaClient:
    """Клиент внешнего API Яги. Не зависит от Sentry."""

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
        self._tokens = TokenManager(
            login=self.login,
            refresh=self.refresh,
            cache=self._cache,
            cache_key=f"{prefix}:token",
        )

    @staticmethod
    def _cache_prefix(instance_url: str, email: str) -> str:
        """Общий префикс ключей кэша для пары «инсталляция + сервисный аккаунт»."""
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
        """Запрос с Bearer-токеном; при 401 один раз перелогинивается.

        Только 401: 403 у Яги означает «токен валиден, но прав на объект нет» —
        релогин его не исправит, лишь добавит запрос и замаскирует настоящую причину.
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

    # --- аутентификация -------------------------------------------------

    def login(self) -> Token:
        payload = self._send(
            "POST", "/v1/auth/login", json={"email": self._email, "password": self._password}
        )
        return Token.from_api(payload)

    def refresh(self, refresh_token: str) -> Token:
        payload = self._send("POST", "/v1/auth/refresh", json={"refreshToken": refresh_token})
        return Token.from_api(payload)

    # --- справочники ----------------------------------------------------

    def get_projects(self) -> list[Project]:
        """Все доступные пространства (со всех страниц), с коротким кэшем.

        Кэш общий с токеном (в проде — Django cache Sentry), поэтому переживает
        отдельный HTTP-запрос: форма линковки дёргает список на каждое нажатие клавиши.
        """
        cached = self._cache.get(self._projects_cache_key)
        if cached is not None:
            return [Project.from_api(item) for item in cached.get("content", [])]

        content = self._fetch_all_projects()
        self._cache.set(self._projects_cache_key, {"content": content}, timeout=PROJECTS_CACHE_TTL)
        return [Project.from_api(item) for item in content]

    def _fetch_all_projects(self) -> list[dict[str, Any]]:
        """Дочитать все страницы `/v1/project/list/my` — 101-е пространство тоже видимо."""
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

    # --- задачи ---------------------------------------------------------

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

    def create_comment(self, task_id: int, content: str) -> None:
        self._authed(
            "POST",
            "/v1/comment",
            json={"taskId": task_id, "contentComment": content, "attachIsPending": False},
        )
