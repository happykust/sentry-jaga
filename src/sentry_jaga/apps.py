"""Django-приложение пакета. Регистрируется через entry point `sentry.apps`.

Нужно, чтобы Django нашёл шаблон установочной формы
(`sentry_jaga/templates/sentry_jaga/config.html`).
"""

from __future__ import annotations

from django.apps import AppConfig


# `django.*` разрешается в Any (mypy: follow_imports = "skip"), а strict-режим
# запрещает наследование от Any — отсюда точечный ignore.
class JagaAppConfig(AppConfig):  # type: ignore[misc]
    name = "sentry_jaga"
    verbose_name = "Sentry Jaga Integration"
