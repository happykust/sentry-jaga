"""Lightweight models on top of Jaga DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def _parse_dt(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp from Jaga (including the `Z` suffix form)."""
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Token:
    access_token: str
    refresh_token: str
    expires_at: datetime

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Token:
        return cls(
            access_token=payload["accessToken"],
            refresh_token=payload["refreshToken"],
            expires_at=_parse_dt(payload["expiresAt"]),
        )

    def is_expired(self, leeway_seconds: int = 30) -> bool:
        remaining = (self.expires_at - datetime.now(UTC)).total_seconds()
        return remaining <= leeway_seconds

    def to_dict(self) -> dict[str, str]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Token:
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=_parse_dt(data["expires_at"]),
        )


@dataclass(frozen=True, slots=True)
class Project:
    """A Jaga space. The Jaga API calls it a "project"; its UI calls it a space."""

    id: int
    title: str
    code: str

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Project:
        return cls(id=payload["id"], title=payload["title"], code=payload.get("code", ""))


@dataclass(frozen=True, slots=True)
class TaskType:
    id: int
    name: str

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> TaskType:
        return cls(id=payload["id"], name=payload["typeName"])


@dataclass(frozen=True, slots=True)
class Attribute:
    id: int
    name: str
    object_type_name_m: str
    dictionary_id: int | None = None
    required: bool = False
    multiple: bool = False
    visible: bool = True
    order_num: int = 0

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Attribute:
        return cls(
            id=payload["id"],
            name=payload.get("name", ""),
            object_type_name_m=payload.get("objectTypeNameM", ""),
            dictionary_id=payload.get("dictionaryId"),
            required=bool(payload.get("required", False)),
            multiple=bool(payload.get("multiple", False))
            or bool(payload.get("multipleSelector", False)),
            visible=bool(payload.get("visible", True)),
            order_num=int(payload.get("orderNum", 0)),
        )


@dataclass(frozen=True, slots=True)
class TaskRef:
    """A task, reduced to what Sentry needs of it: the id, the code, the title.

    No `from_api`, unlike the other models: Jaga has no single DTO this maps from. A create
    answers with a `TaskApiDto` carrying no title at all, and the global search answers with a
    reduced DTO whose title is buried in the EAV `attributes`, so each caller builds its own.
    """

    id: int
    code: str
    title: str


@dataclass(frozen=True, slots=True)
class Status:
    """A task status inside one workflow.

    `category` is `categoryNameM`, the mnemonic of the status *category* (`status.category.done`
    and friends) — the only part of a status that is stable instance-wide: a live instance carries
    ~90k statuses over ~15k workflows, all variations on the same handful of categories. So the
    status sync keys on the category and resolves the `id` per space; see
    `issue_config.resolve_target_status`.
    """

    id: int
    name: str
    category: str

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Status:
        return cls(
            id=int(payload["id"]),
            name=str(payload.get("name") or ""),
            category=str(payload.get("categoryNameM") or ""),
        )


@dataclass(frozen=True, slots=True)
class Person:
    """A Jaga user — and the reason this class exists: one human has TWO unrelated numeric ids.

    Jaga is two services behind one API, each keeping its own id:

    * `uuid`    — `personUuid`, the cross-system identifier and the ONLY thing the
                  `task.assignee_uuid` attribute takes.
    * `core_id` — "Идентификатор пользователя (Core)". What a task's `executors` are keyed by.
    * `team_id` — "Идентификатор пользователя (Team)".

    The trap: the user-role matrix returns the *team* id in a field called plain `id`, so
    `member["id"]` is not "the user id" and nothing finds out until Jaga 404s. Naming all three
    here means nothing downstream passes a bare int around.

    Only `POST /v1/team/userProfile/findByMailOrName` returns all three at once, keyed by email —
    which is why the plugin carries emails between Sentry and Jaga.
    """

    uuid: str
    core_id: int
    team_id: int
    email: str
    name: str

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Person:
        """Build from a `UserProfileShortApiDto` — note it spells the email `mail`."""
        email = str(payload.get("mail") or "")
        return cls(
            uuid=str(payload.get("uuid") or ""),
            core_id=int(payload.get("coreId") or 0),
            team_id=int(payload.get("teamId") or 0),
            email=email,
            name=str(payload.get("fullName") or email),
        )
