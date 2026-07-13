import pytest
import responses
from django.core.exceptions import ValidationError

# The requests ConnectionError, not the builtin one: the network must fail the way
# requests sees it (RequestException) — that is exactly what verify_credentials catches.
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
        responses.POST, f"{API}/v1/auth/login", json={"message": "Invalid password"}, status=401
    )
    with pytest.raises(ValidationError, match="rejected the credentials"):
        verify_credentials(BASE, "bot@example.com", "wrong")


@responses.activate
def test_verify_credentials_reports_unreachable_instance() -> None:
    responses.add(responses.POST, f"{API}/v1/auth/login", body=ConnectionError("no route"))
    with pytest.raises(ValidationError, match="Could not connect to Jaga"):
        verify_credentials(BASE, "bot@example.com", "secret")


@responses.activate
def test_verify_credentials_reports_api_error() -> None:
    """Jaga is reachable and the account was not rejected, but the API errored — not a 401."""
    responses.add(
        responses.POST, f"{API}/v1/auth/login", json={"message": "Internal error"}, status=500
    )
    with pytest.raises(ValidationError, match="Jaga returned an error on login"):
        verify_credentials(BASE, "bot@example.com", "secret")


@responses.activate
def test_form_valid_when_jaga_accepts_credentials() -> None:
    """The form is valid only once Jaga has confirmed the account with a live login."""
    responses.add(responses.POST, f"{API}/v1/auth/login", json=LOGIN_OK, status=200)
    form = InstallationForm(
        {"instance_url": BASE, "email": "bot@example.com", "password": "secret"}
    )

    assert form.is_valid(), form.errors
    assert [call.request.url for call in responses.calls] == [f"{API}/v1/auth/login"]


@responses.activate
def test_form_assumes_https_for_schemeless_url() -> None:
    """A schemeless address is completed to HTTPS, not to HTTP.

    On Django 5.x (which is what Sentry 26.3.1 ships), a `URLField` without `assume_scheme`
    substitutes `http://`. The service account password would then go out for verification
    in plain text, and the http address would settle into `Integration.metadata` forever.
    """
    responses.add(responses.POST, f"{API}/v1/auth/login", json=LOGIN_OK, status=200)
    form = InstallationForm(
        {"instance_url": "jaga.example.com", "email": "bot@example.com", "password": "secret"}
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["instance_url"] == "https://jaga.example.com"
    # And the login itself went over HTTPS — the password never leaves the box in the clear.
    assert [call.request.url for call in responses.calls] == [f"{API}/v1/auth/login"]


@responses.activate
def test_form_surfaces_rejection_by_jaga_as_form_error() -> None:
    responses.add(
        responses.POST, f"{API}/v1/auth/login", json={"message": "Invalid password"}, status=401
    )
    form = InstallationForm({"instance_url": BASE, "email": "bot@example.com", "password": "wrong"})

    # dict() on purpose: str(form.errors) renders HTML and needs Django apps to be loaded.
    assert not form.is_valid()
    assert "rejected the credentials" in dict(form.errors)["__all__"][0]


@responses.activate
def test_form_does_not_call_jaga_when_a_field_is_missing() -> None:
    """Without a password there is nothing to check — do not call Jaga (that login is
    known-broken up front)."""
    form = InstallationForm({"instance_url": BASE, "email": "bot@example.com"})

    assert not form.is_valid()
    assert dict(form.errors) == {"password": ["This field is required."]}
    assert len(responses.calls) == 0
