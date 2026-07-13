"""Django app for the package. Registered through the `sentry.apps` entry point.

Required so that Django can find the installation form template
(`sentry_jaga/templates/sentry_jaga/config.html`).
"""

from __future__ import annotations

from django.apps import AppConfig


class JagaAppConfig(AppConfig):
    name = "sentry_jaga"
    verbose_name = "Sentry Jaga Integration"
