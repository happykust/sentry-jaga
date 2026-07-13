"""Django-приложение пакета. Регистрируется через entry point `sentry.apps`.

Нужно, чтобы Django нашёл шаблон установочной формы
(`sentry_jaga/templates/sentry_jaga/config.html`).
"""

from __future__ import annotations

from django.apps import AppConfig


class JagaAppConfig(AppConfig):
    name = "sentry_jaga"
    verbose_name = "Sentry Jaga Integration"
