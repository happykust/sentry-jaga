import pytest
import responses
from django.core.exceptions import ValidationError

# Именно requests-овый ConnectionError, а не встроенный: сеть должна падать так,
# как её видит requests (RequestException), — это и ловит verify_credentials.
from requests.exceptions import ConnectionError

from sentry_jaga.pipeline import InstallationForm, verify_credentials

BASE = "https://jaga.example.com"
API = f"{BASE}/external-api"

LOGIN_OK = {
    "accessToken": "at",
    "refreshToken": "rt",
    "expiresAt": "2099-01-01T00:00:00Z",
    "id": 1,
    "email": "bot@example.com",
    "fullName": "Bot",
}


@responses.activate
def test_verify_credentials_ok() -> None:
    responses.add(responses.POST, f"{API}/v1/auth/login", json=LOGIN_OK, status=200)
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


@responses.activate
def test_form_valid_when_jaga_accepts_credentials() -> None:
    """Форма валидна только после того, как Яга подтвердила учётку живым логином."""
    responses.add(responses.POST, f"{API}/v1/auth/login", json=LOGIN_OK, status=200)
    form = InstallationForm(
        {"instance_url": BASE, "email": "bot@example.com", "password": "secret"}
    )

    assert form.is_valid(), form.errors
    assert [call.request.url for call in responses.calls] == [f"{API}/v1/auth/login"]


@responses.activate
def test_form_surfaces_rejection_by_jaga_as_form_error() -> None:
    responses.add(
        responses.POST, f"{API}/v1/auth/login", json={"message": "Неверный пароль"}, status=401
    )
    form = InstallationForm({"instance_url": BASE, "email": "bot@example.com", "password": "wrong"})

    # Именно dict(): str(form.errors) рендерит HTML и требует загруженных app-ов Django.
    assert not form.is_valid()
    assert "учётные данные" in dict(form.errors)["__all__"][0]


@responses.activate
def test_form_does_not_call_jaga_when_a_field_is_missing() -> None:
    """Без пароля проверять нечего — в Ягу не ходим (иначе шлём заведомо битый логин)."""
    form = InstallationForm({"instance_url": BASE, "email": "bot@example.com"})

    assert not form.is_valid()
    assert dict(form.errors) == {"password": ["This field is required."]}
    assert len(responses.calls) == 0
