from sentry_jaga.descriptions import (
    UNKNOWN_AUTHOR,
    build_description,
    build_link_comment,
    build_note_comment,
    build_title,
)

URL = "https://sentry.example.com/organizations/acme/issues/42/"


def test_build_title_passes_group_title_through() -> None:
    assert build_title("TypeError: invalid type") == "TypeError: invalid type"


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


# --- a Sentry note, as it lands on the Jaga task --------------------------------------------


def test_note_comment_attributes_the_note_to_its_author() -> None:
    """Without the attribution line every synced note would read as if the service account had
    written it."""
    text = build_note_comment("Ivanov Ivan", "Looks like a bad deploy")

    assert text == "Ivanov Ivan wrote:\n\n> Looks like a bad deploy"


def test_note_comment_quotes_every_line_of_a_multi_line_note() -> None:
    """A single leading ">" would quote the first line only, leaving the rest to read as Jaga's own
    text."""
    text = build_note_comment("Ivan", "first\nsecond")

    assert text == "Ivan wrote:\n\n> first\n> second"


def test_note_comment_keeps_blank_lines_inside_the_quote() -> None:
    """An unprefixed empty line ends a Markdown blockquote, dropping the paragraph after it out of
    the quotation."""
    text = build_note_comment("Ivan", "first\n\nsecond")

    assert text == "Ivan wrote:\n\n> first\n>\n> second"


def test_note_comment_falls_back_when_the_author_is_unknown() -> None:
    """A note can outlive the account that wrote it, and an empty attribution ("wrote:") would read
    as though Jaga itself spoke."""
    assert build_note_comment("", "hi").startswith(f"{UNKNOWN_AUTHOR} wrote:")


def test_note_comment_of_an_empty_note_is_just_the_attribution() -> None:
    assert build_note_comment("Ivan", "") == "Ivan wrote:"


# --- the comment posted when a task is linked ----------------------------------------------


def test_link_comment_points_back_at_the_sentry_issue() -> None:
    assert build_link_comment(URL) == f"Linked to Sentry issue {URL}"
