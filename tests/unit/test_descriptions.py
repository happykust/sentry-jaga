from sentry_jaga.descriptions import build_description, build_title

URL = "https://sentry.example.com/organizations/acme/issues/42/"


def test_build_title_passes_group_title_through() -> None:
    assert build_title("TypeError: неверный тип") == "TypeError: неверный тип"


def test_build_title_truncates_long_titles() -> None:
    title = build_title("x" * 300)
    assert len(title) == 255
    assert title.endswith("…")


def test_build_description_contains_link_and_body() -> None:
    text = build_description(URL, "app/views.py in login", "Traceback ...")
    assert URL in text
    assert "app/views.py in login" in text
    assert "Traceback ..." in text


def test_build_description_without_body() -> None:
    text = build_description(URL, "app/views.py", "")
    assert URL in text
    assert "```" not in text


def test_build_description_without_culprit() -> None:
    text = build_description(URL, "", "boom")
    assert URL in text
    assert "boom" in text
