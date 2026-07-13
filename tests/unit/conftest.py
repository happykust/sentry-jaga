"""Minimal Django settings for the unit tests.

At runtime the settings come from Sentry, but `sentry_jaga.pipeline` only depends on
`django.forms`. Importing it and calling `verify_credentials` works without settings,
whereas `Form.is_valid()` reaches for translations (USE_I18N) and fails with
ImproperlyConfigured — hence the minimal Django configuration. The `sentry` package is not
needed for this.
"""

from django.conf import settings

if not settings.configured:
    settings.configure(USE_I18N=False)
