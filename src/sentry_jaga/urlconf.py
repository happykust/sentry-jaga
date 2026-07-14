"""A drop-in `ROOT_URLCONF` that adds the Jaga routes to Sentry's own.

Point Sentry at it from `sentry.conf.py`:

    ROOT_URLCONF = "sentry_jaga.urlconf"

It is OPTIONAL: without it the integration works, and only the live task search when linking an
issue falls back to the slower `updatesForm` behaviour. See
`issues.py::JagaIssuesMixin._search_url`.

Ours come first, and nothing of Sentry's is shadowed: `^extensions/jaga/…` is a prefix Sentry does
not use, and `sentry_urlpatterns` is spliced in whole.
"""

from __future__ import annotations

from sentry.conf.urls import urlpatterns as sentry_urlpatterns

from sentry_jaga.urls import urlpatterns as jaga_urlpatterns

urlpatterns = [*jaga_urlpatterns, *sentry_urlpatterns]
