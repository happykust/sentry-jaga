"""Минимальные настройки Django для юнит-тестов.

В рантайме settings приходят от Sentry, но `sentry_jaga.pipeline` зависит только от
`django.forms`. Импорт и `verify_credentials` работают и без settings, а вот
`Form.is_valid()` дёргает переводы (USE_I18N) и падает на ImproperlyConfigured —
поэтому конфигурируем Django минимально. Пакет `sentry` для этого не нужен.
"""

from django.conf import settings

if not settings.configured:
    settings.configure(USE_I18N=False)
