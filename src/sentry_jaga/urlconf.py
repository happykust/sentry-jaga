"""A drop-in `ROOT_URLCONF` that adds the Jaga routes to Sentry's own.

Point Sentry at it from `sentry.conf.py`:

    ROOT_URLCONF = "sentry_jaga.urlconf"

That is the whole installation. It is OPTIONAL: without it the integration works, and only the
live task search when linking an issue falls back to the slower `updatesForm` behaviour (the
form is re-fetched as you type). See `issues.py::JagaIssuesMixin._search_url`.

Ours come first. Django resolves in order and stops at the first match, so a pattern of ours
could in principle shadow one of Sentry's — hence `^extensions/jaga/…`, a prefix Sentry does not
use. The reverse order would be equally correct here; first is simply cheaper to reason about,
because it means our own patterns can never be shadowed *by* Sentry. Nothing is removed from
Sentry's urlconf either way: `sentry_urlpatterns` is spliced in whole.
"""

from __future__ import annotations

from sentry.conf.urls import urlpatterns as sentry_urlpatterns

from sentry_jaga.urls import urlpatterns as jaga_urlpatterns

urlpatterns = [*jaga_urlpatterns, *sentry_urlpatterns]
