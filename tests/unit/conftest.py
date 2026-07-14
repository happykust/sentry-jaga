"""Minimal Django settings for the unit tests: `Form.is_valid()` reaches for translations and
fails with ImproperlyConfigured without them. The `sentry` package is not needed."""

from django.conf import settings

if not settings.configured:
    settings.configure(USE_I18N=False)
