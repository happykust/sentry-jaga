import pytest

from sentry_jaga.client.exceptions import (
    JagaApiError,
    JagaAuthError,
    JagaNotFoundError,
    JagaRateLimitedError,
    JagaServerError,
    error_from_response,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, JagaAuthError),
        (403, JagaAuthError),
        (404, JagaNotFoundError),
        (429, JagaRateLimitedError),
        (500, JagaServerError),
        (503, JagaServerError),
        (400, JagaApiError),
    ],
)
def test_error_from_response_maps_status(status: int, expected: type[JagaApiError]) -> None:
    err = error_from_response(status, {"message": "boom"})
    assert isinstance(err, expected)
    assert err.status_code == status


def test_error_message_uses_body_message() -> None:
    err = error_from_response(400, {"message": "This field is required"})
    assert "This field is required" in str(err)


def test_error_message_falls_back_when_body_unparseable() -> None:
    err = error_from_response(500, "boom")
    assert "500" in str(err)
