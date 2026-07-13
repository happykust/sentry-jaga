"""Autocomplete endpoint for the "link an existing task" form.

Sentry's frontend turns a `select` field into an async one as soon as the field dict carries a
`url` (see `getFieldProps` in static/app/components/externalIssues/utils.tsx). It then calls
that URL on every keystroke — debounced, which is the whole point — as

    GET <url>?<values of every updatesForm field>&field=<field name>&query=<what was typed>

The space select is an `updatesForm` field, so its value arrives here as `project`. Jaga cannot
search without it: `/v1/task/searchByTitleCode` requires a `projectId`.

The endpoint is only reachable if the admin has pointed `ROOT_URLCONF` at `sentry_jaga.urlconf`.
When they have not, `get_link_issue_config` never puts a `url` on the field and the form falls
back to the old `updatesForm` search — see `issues.py`.
"""

from __future__ import annotations

from typing import Any

from rest_framework.request import Request
from rest_framework.response import Response
from sentry.api.api_owners import ApiOwner
from sentry.api.api_publish_status import ApiPublishStatus
from sentry.api.base import control_silo_endpoint
from sentry.api.paginator import SequencePaginator
from sentry.integrations.api.bases.integration import IntegrationEndpoint
from sentry.integrations.models.integration import Integration
from sentry.organizations.services.organization import RpcOrganization
from sentry.shared_integrations.exceptions import IntegrationError

from sentry_jaga.issue_config import MIN_QUERY_LENGTH

# The only field of the link form that searches. `parent`, which Jira also serves here, has no
# equivalent in the Jaga form.
SEARCHABLE_FIELD = "externalIssue"


@control_silo_endpoint
class JagaSearchEndpoint(IntegrationEndpoint):
    owner = ApiOwner.INTEGRATIONS
    # Plain assignment, exactly as Sentry's own endpoints declare it. Neither `ClassVar` nor an
    # annotation of our own will do: `Endpoint` declares this as an *instance* variable keyed by
    # a Literal of HTTP methods, so both are rejected when we type-check against the real Sentry.
    # In this repo's standalone run the base resolves to Any, and ruff sees only a mutable class
    # attribute — hence the noqa rather than a type it would reject upstream.
    publish_status = {"GET": ApiPublishStatus.PRIVATE}  # noqa: RUF012
    provider = "jaga"

    def get(
        self,
        request: Request,
        organization: RpcOrganization,
        integration_id: int,
        **kwds: Any,
    ) -> Response:
        try:
            integration = Integration.objects.get(
                organizationintegration__organization_id=organization.id,
                id=integration_id,
                provider=self.provider,
            )
        except Integration.DoesNotExist:
            return Response(status=404)

        field = request.GET.get("field")
        if field != SEARCHABLE_FIELD:
            # Includes `field` being absent altogether: there is nothing else to search here,
            # and answering [] would turn a wiring bug into an empty dropdown nobody can debug.
            return Response(
                {"detail": f"Unsupported field: {field!r}. Only {SEARCHABLE_FIELD!r} is searched."},
                status=400,
            )

        project = request.GET.get("project")
        if not (project or "").isdigit():
            return Response(
                {"detail": "project is a required parameter: Jaga searches within one space."},
                status=400,
            )

        choices: list[dict[str, str]] = []
        query = (request.GET.get("query") or "").strip()

        # A query below the minimum is not an error — the frontend fires once with an empty input
        # when the field mounts, and again on the first keystroke. It just is not worth asking
        # Jaga about, so we answer an empty list without touching it.
        if len(query) >= MIN_QUERY_LENGTH:
            installation = integration.get_installation(organization.id)
            try:
                # `search_issues` is ours (issues.py) and already speaks Sentry's error dialect.
                results = installation.search_issues(query, project_id=int(project))
            except IntegrationError as exc:
                return Response({"detail": str(exc)}, status=400)

            choices = [
                {"label": f"{task['key']} — {task['title']}", "value": task["key"]}
                for task in results
            ]

        # Every list response leaves through here, paginated — including the empty one. Sentry
        # rejects an unpaginated list from a GET endpoint outright (`MissingPaginationError` in
        # `Endpoint.dispatch`). Its own integration search endpoints predate that rule and sit in
        # `SENTRY_API_PAGINATION_ALLOWLIST_DO_NOT_MODIFY`, a list whose docstring says in as many
        # words: DO NOT ADD ANY NEW APIS. So we do it the way Sentry now wants it done.
        #
        # The body stays a plain JSON array of {label, value} — all the frontend reads — and only
        # Link headers are added. The index is the score, so Jaga's ordering by relevance survives
        # (`SequencePaginator` sorts by it).
        return self.paginate(
            request=request,
            paginator=SequencePaginator(list(enumerate(choices))),
            on_results=lambda rows: rows,
        )
