"""Формирование заголовка и описания задачи Яги из данных Sentry-issue."""

from __future__ import annotations

MAX_TITLE_LENGTH = 255


def build_title(group_title: str) -> str:
    """Заголовок задачи. Яга ограничивает длину — обрезаем аккуратно."""
    if len(group_title) <= MAX_TITLE_LENGTH:
        return group_title
    return group_title[: MAX_TITLE_LENGTH - 1] + "…"


def build_description(sentry_url: str, culprit: str, body: str) -> str:
    """Описание задачи: ссылка на Sentry-issue, место падения и тело события."""
    lines = [f"Sentry-issue: {sentry_url}"]
    if culprit:
        lines.append(f"Место: {culprit}")
    if body:
        lines.extend(["", "```", body, "```"])
    return "\n".join(lines)
