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


def test_error_message_unwraps_the_json_string_jaga_nests_in_error() -> None:
    """Jaga hides the real message one level down, as a JSON string inside `error`: taken at face
    value, the user gets a JSON sheet instead of the one sentence that matters."""
    body = {
        "timestamp": "2026-07-13T10:00:00.000+00:00",
        "status": 500,
        "error": (
            '{"status":500,'
            '"message":"Поле \\"Пространство\\" обязательно для заполнения '
            'для типа задачи = 33532",'
            '"path":"/external-api/v1/task/createByTaskType/1/33532"}'
        ),
    }

    err = error_from_response(500, body)

    assert 'Поле "Пространство" обязательно для заполнения для типа задачи = 33532' in str(err)
    assert "timestamp" not in str(err)
    assert '"path"' not in str(err)


def test_error_message_keeps_a_plain_error_string_as_is() -> None:
    """Not every `error` is JSON — one that does not parse is already the message."""
    err = error_from_response(500, {"error": "Internal Server Error"})
    assert "Internal Server Error" in str(err)


def test_error_message_falls_back_when_body_unparseable() -> None:
    err = error_from_response(500, "boom")
    assert "500" in str(err)
