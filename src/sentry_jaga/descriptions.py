"""Building the title and description of a Jaga task from Sentry issue data."""

from __future__ import annotations

MAX_TITLE_LENGTH = 255


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
