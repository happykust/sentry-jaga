import pytest
import responses
from django.core.exceptions import ValidationError

# Именно requests-овый ConnectionError, а не встроенный: сеть должна падать так,
# как её видит requests (RequestException), — это и ловит verify_credentials.
from requests.exceptions import ConnectionError

from sentry_jaga.pipeline import verify_credentials

BASE = "https://jaga.example.com"
API = f"{BASE}/external-api"


@responses.activate
def test_verify_credentials_ok() -> None:
    responses.add(
        responses.POST,
        f"{API}/v1/auth/login",
        json={
            "accessToken": "at",
            "refreshToken": "rt",
            "expiresAt": "2099-01-01T00:00:00Z",
            "id": 1,
            "email": "bot@example.com",
            "fullName": "Bot",
        },
        status=200,
    )
    verify_credentials(BASE, "bot@example.com", "secret")


@responses.activate
def test_verify_credentials_rejects_bad_password() -> None:
    responses.add(
        responses.POST, f"{API}/v1/auth/login", json={"message": "Неверный пароль"}, status=401
    )
    with pytest.raises(ValidationError, match="учётные данные"):
        verify_credentials(BASE, "bot@example.com", "wrong")


@responses.activate
def test_verify_credentials_reports_unreachable_instance() -> None:
    responses.add(responses.POST, f"{API}/v1/auth/login", body=ConnectionError("no route"))
    with pytest.raises(ValidationError, match="Не удалось подключиться"):
        verify_credentials(BASE, "bot@example.com", "secret")


@responses.activate
def test_verify_credentials_reports_api_error() -> None:
    """Яга доступна и учётка не отвергнута, но API ответил ошибкой — это не 401."""
    responses.add(
        responses.POST, f"{API}/v1/auth/login", json={"message": "Внутренняя ошибка"}, status=500
    )
    with pytest.raises(ValidationError, match="Яга вернула ошибку при входе"):
        verify_credentials(BASE, "bot@example.com", "secret")
