from datetime import UTC, datetime, timedelta

from sentry_jaga.client.models import Attribute, Project, TaskRef, TaskType, Token

AUTH_PAYLOAD = {
    "accessToken": "at",
    "refreshToken": "rt",
    "expiresAt": "2026-06-25T12:00:00Z",
    "id": 1,
    "email": "bot@example.com",
    "fullName": "Bot",
}


def test_token_from_api() -> None:
    token = Token.from_api(AUTH_PAYLOAD)
    assert token.access_token == "at"
    assert token.refresh_token == "rt"
    assert token.expires_at == datetime(2026, 6, 25, 12, 0, tzinfo=UTC)


def test_token_is_expired_respects_leeway() -> None:
    soon = datetime.now(UTC) + timedelta(seconds=10)
    token = Token(access_token="at", refresh_token="rt", expires_at=soon)
    assert token.is_expired(leeway_seconds=30) is True
    assert token.is_expired(leeway_seconds=0) is False


def test_token_roundtrip_dict() -> None:
    token = Token.from_api(AUTH_PAYLOAD)
    assert Token.from_dict(token.to_dict()) == token


def test_project_from_api() -> None:
    project = Project.from_api({"id": 7, "title": "Платформа", "code": "PLT"})
    assert (project.id, project.title, project.code) == (7, "Платформа", "PLT")


def test_task_type_from_api() -> None:
    task_type = TaskType.from_api({"id": 3, "typeName": "Баг"})
    assert (task_type.id, task_type.name) == (3, "Баг")


def test_attribute_from_api_defaults() -> None:
    attr = Attribute.from_api({"id": 11, "name": "Название", "objectTypeNameM": "task.title"})
    assert attr.id == 11
    assert attr.object_type_name_m == "task.title"
    assert attr.required is False
    assert attr.multiple is False
    assert attr.visible is True
    assert attr.dictionary_id is None


def test_attribute_from_api_dictionary_and_multiple() -> None:
    attr = Attribute.from_api(
        {
            "id": 12,
            "name": "Приоритет",
            "objectTypeNameM": "task.priority",
            "dictionaryId": 55,
            "required": True,
            "multiple": True,
            "multipleSelector": True,
            "visible": True,
            "orderNum": 2,
        }
    )
    assert attr.dictionary_id == 55
    assert attr.required is True
    assert attr.multiple is True
    assert attr.order_num == 2


def test_task_ref_from_api() -> None:
    ref = TaskRef.from_api({"id": 5, "code": "PLT-5", "title": "Падает логин"})
    assert (ref.id, ref.code, ref.title) == (5, "PLT-5", "Падает логин")
