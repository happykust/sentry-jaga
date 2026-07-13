"""URLs the package adds to Sentry.

Sentry has no hook for an out-of-tree package to contribute routes: `sentry.web.urls` is a flat
module, and the one plugin hook that does mount URLs (`IPlugin2.get_url_module`, wired up in
`sentry/plugins/base/urls.py`) is only ever consulted for *v1* plugins — `plugins.all()` defaults
to `version=1`. So the admin points `ROOT_URLCONF` at `sentry_jaga.urlconf`, which is a plain
Django setting and stacks these patterns on top of Sentry's own.

The path mirrors Jira Server's (`/extensions/jira-server/search/<org>/<integration_id>/`), which
is where a Sentry reader would look for it.
"""

from __future__ import annotations

from django.urls import re_path

from sentry_jaga.search import JagaSearchEndpoint

urlpatterns = [
    re_path(
        r"^extensions/jaga/search/(?P<organization_id_or_slug>[^/]+)/(?P<integration_id>\d+)/$",
        JagaSearchEndpoint.as_view(),
        name="sentry-jaga-search",
    ),
]
