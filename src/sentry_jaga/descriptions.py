"""Building the text a Jaga task carries: its title, its description, and its comments."""

from __future__ import annotations

MAX_TITLE_LENGTH = 255

# Attribution for a note whose Sentry user can no longer be resolved (a deleted account). Without
# it the note would read as if the service account itself had spoken.
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

    Every comment is created by the service account, so without the attribution line a discussion
    of three people would collapse into one voice (Jira Server does the same). The body is quoted
    line by line, since a single leading `>` would quote only the first line of a multi-line note.
    """
    author = author_name.strip() or UNKNOWN_AUTHOR
    quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
    if not quoted:
        return f"{author} wrote:"
    return f"{author} wrote:\n\n{quoted}"


def build_link_comment(sentry_url: str) -> str:
    """The comment posted on a Jaga task when an existing task is linked to a Sentry issue.

    Only the default of an editable field in the link form (see `issue_config.build_link_config`):
    the user may reword or clear it, so it must read well with no other context.
    """
    return f"Linked to Sentry issue {sentry_url}"
