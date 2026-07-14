"""Building the text a Jaga task carries: its title, its description, and its comments."""

from __future__ import annotations

MAX_TITLE_LENGTH = 255

# The author of a note whose Sentry user cannot be resolved any more — a deleted account, or
# one the note outlived. The note still has to reach Jaga, and it still has to say that a human
# on the Sentry side wrote it; an empty attribution line would read as if Jaga itself spoke.
UNKNOWN_AUTHOR = "A Sentry user"


def build_title(group_title: str) -> str:
    """Task title. Jaga caps its length, so truncate it gracefully."""
    if len(group_title) <= MAX_TITLE_LENGTH:
        return group_title
    return group_title[: MAX_TITLE_LENGTH - 1] + "…"


def build_description(sentry_url: str, culprit: str, body: str) -> str:
    """Task description: a link to the Sentry issue, the culprit and the event body."""
    lines = [f"Sentry issue: {sentry_url}"]
    if culprit:
        lines.append(f"Culprit: {culprit}")
    if body:
        lines.extend(["", "```", body, "```"])
    return "\n".join(lines)


def build_note_comment(author_name: str, text: str) -> str:
    """A Sentry note, as it is posted on the linked Jaga task.

    The attribution line is the whole point: the comment is created by the *service account*, so
    without it every note in Jaga would look as if the bot had written it, and a discussion of
    three people would collapse into one voice. Jira Server does the same
    (`create_comment_attribution`), and quotes the body — here with a Markdown blockquote, the
    flavour `build_description` above already bets on.

    The text is quoted line by line rather than wrapped once: a note is free-form and routinely
    multi-line, and a single leading `>` would quote only its first line.
    """
    author = author_name.strip() or UNKNOWN_AUTHOR
    quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
    if not quoted:
        return f"{author} wrote:"
    return f"{author} wrote:\n\n{quoted}"


def build_link_comment(sentry_url: str) -> str:
    """The comment posted on a Jaga task when an existing task is linked to a Sentry issue.

    This is only the *default* of an editable field in the link form — the user may reword it or
    clear it out entirely (see `issue_config.build_link_config`), exactly as in Jira Server. So
    it has to read well on its own, with no other context, on a task whose watchers have never
    heard of Sentry.
    """
    return f"Linked to Sentry issue {sentry_url}"
