"""The task-search endpoint and the two shapes of the link form, against a real Sentry.

The endpoint only exists when the admin has pointed `ROOT_URLCONF` at `sentry_jaga.urlconf`
(see `urlconf.py`), so these tests do the same thing an admin would, with `override_settings`.
That is also what makes the degradation test honest: it asserts the fallback under Sentry's own
urlconf, where the route genuinely is not registered.
"""

import pytest

pytest.importorskip("sentry")

import responses
from django.test import override_settings
from django.urls import reverse
from sentry.silo.base import SiloMode
from sentry.testutils.cases import APITestCase
from sentry.testutils.silo import assume_test_silo_mode, control_silo_test

from sentry_jaga.issue_config import MIN_QUERY_LENGTH

BASE = "https://jaga.example.com"
API = f"{BASE}/external-api"
URLCONF = "sentry_jaga.urlconf"

AUTH_OK = {
    "accessToken": "at",
    "refreshToken": "rt",
    "expiresAt": "2099-01-01T00:00:00Z",
    "id": 1,
    "email": "bot@example.com",
    "fullName": "Bot",
}
# The global search, exactly as a live instance answers it: a page OBJECT (the spec claims an
# array), a title that lives in the EAV `attributes`, and a space that comes back null.
SEARCH_RESULT = {
    "content": [
        {
            "id": 5,
            "code": "PLT-5",
            "projectId": None,
            "projectCode": None,
            "projectTitle": None,
            "attributes": [
                {"fieldId": 100, "value": "Login is broken", "objectTypeNameM": "task.task_title"}
            ],
        }
    ],
    "totalPages": 1,
    "pageNumber": 0,
    "totalElements": 1,
}


# The endpoint is `@control_silo_endpoint`, and `Integration` is a control-silo model.
@control_silo_test
class JagaSearchEndpointTest(APITestCase):
    def setUp(self) -> None:
        super().setUp()
        with assume_test_silo_mode(SiloMode.CONTROL):
            self.integration = self.create_provider_integration(
                provider="jaga",
                name="Jaga",
                external_id=BASE,
                metadata={"instance_url": BASE, "email": "bot@example.com", "password": "secret"},
            )
            self.integration.add_organization(self.organization, self.user)
        self.login_as(self.user)

    def _url(self) -> str:
        # `reverse` only resolves under our urlconf — which is the point of the whole feature.
        with override_settings(ROOT_URLCONF=URLCONF):
            url: str = reverse(
                "sentry-jaga-search", args=[self.organization.slug, self.integration.id]
            )
        return url

    def test_the_route_only_exists_under_our_urlconf(self) -> None:
        """Sentry's own urlconf knows nothing of it — that is exactly why the link form has to
        degrade gracefully."""
        from django.urls import NoReverseMatch

        assert "/extensions/jaga/search/" in self._url()

        with pytest.raises(NoReverseMatch):
            reverse("sentry-jaga-search", args=[self.organization.slug, self.integration.id])

    @responses.activate
    def test_it_searches_every_space_and_returns_pairs_the_frontend_understands(self) -> None:
        """No space is sent, and none is needed: the endpoint used to demand a `project` because
        Jaga's per-space search wants a `projectId`. The global search does not."""
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.POST, f"{API}/v1/globalSearch/findTaskList", json=SEARCH_RESULT)

        with override_settings(ROOT_URLCONF=URLCONF):
            resp = self.client.get(self._url(), {"field": "externalIssue", "query": "login"})

        assert resp.status_code == 200
        # `code — title` and nothing else: the global search returns the task's space as null.
        assert resp.data == [{"label": "PLT-5 — Login is broken", "value": "PLT-5"}]

        search = next(c for c in responses.calls if "globalSearch" in c.request.url)
        assert "query=login" in search.request.url
        # And the space list was never fetched — linking no longer needs it at all.
        assert not [c for c in responses.calls if "project/list/my" in c.request.url]

    @responses.activate
    def test_a_short_query_is_not_an_error_and_does_not_reach_jaga(self) -> None:
        """The frontend fires once with an empty input when the field mounts, and again on the
        first keystroke. Neither is worth an HTTP call to Jaga."""
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.POST, f"{API}/v1/globalSearch/findTaskList", json=SEARCH_RESULT)

        with override_settings(ROOT_URLCONF=URLCONF):
            resp = self.client.get(
                self._url(),
                {"field": "externalIssue", "query": "l" * (MIN_QUERY_LENGTH - 1)},
            )

        assert resp.status_code == 200
        assert resp.data == []
        assert not [c for c in responses.calls if "globalSearch" in c.request.url]

    def test_an_unknown_field_is_rejected(self) -> None:
        with override_settings(ROOT_URLCONF=URLCONF):
            resp = self.client.get(self._url(), {"field": "nope", "query": "login"})
        assert resp.status_code == 400

    def test_an_unknown_integration_is_a_404(self) -> None:
        with override_settings(ROOT_URLCONF=URLCONF):
            url = reverse("sentry-jaga-search", args=[self.organization.slug, self.integration.id])
            bogus = url.replace(f"/{self.integration.id}/", "/123456/")
            resp = self.client.get(bogus, {"field": "externalIssue", "q": "x"})
        assert resp.status_code == 404


class JagaLinkFormShapeTest(APITestCase):
    """`get_link_issue_config` must produce an async select when the route is installed, and the
    old `updatesForm` search when it is not. The branch is decided by a `reverse()` that either
    resolves or raises — so it can only be tested for real by swapping the urlconf."""

    def setUp(self) -> None:
        super().setUp()
        with assume_test_silo_mode(SiloMode.CONTROL):
            self.integration = self.create_provider_integration(
                provider="jaga",
                name="Jaga",
                external_id=BASE,
                metadata={"instance_url": BASE, "email": "bot@example.com", "password": "secret"},
            )
            self.integration.add_organization(self.organization, self.user)
        self.installation = self.integration.get_installation(self.organization.id)
        self.group = self.create_group(project=self.project, message="Login is broken")

    @staticmethod
    def _mock_jaga() -> None:
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.POST, f"{API}/v1/globalSearch/findTaskList", json=SEARCH_RESULT)

    @responses.activate
    def test_with_the_urlconf_the_task_field_is_a_live_autocomplete(self) -> None:
        self._mock_jaga()

        with override_settings(ROOT_URLCONF=URLCONF):
            config = self.installation.get_link_issue_config(self.group, params={})
        by_name = {field["name"]: field for field in config}

        assert (
            f"/extensions/jaga/search/{self.organization.slug}/{self.integration.id}/"
            == (by_name["externalIssue"]["url"])
        )
        # The `query` box only exists to stand in for autocomplete; with a URL it must be gone.
        assert "query" not in by_name
        # And rendering the form searched nothing — the endpoint does that now.
        assert not [c for c in responses.calls if "globalSearch" in c.request.url]

    @responses.activate
    def test_without_the_urlconf_it_falls_back_to_the_updates_form_search(self) -> None:
        """Sentry's stock urlconf: `reverse` raises, and the form must still work."""
        self._mock_jaga()

        config = self.installation.get_link_issue_config(self.group, params={"query": "login"})
        by_name = {field["name"]: field for field in config}

        assert "url" not in by_name["externalIssue"]
        assert by_name["query"]["updatesForm"] is True
        assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]

    @responses.activate
    def test_neither_shape_of_the_link_form_asks_for_a_space(self) -> None:
        """The point of the whole change: you type, and the task is found wherever it lives. The
        space select is gone — and with it the list of spaces the form used to fetch to build it."""
        self._mock_jaga()

        with override_settings(ROOT_URLCONF=URLCONF):
            live = self.installation.get_link_issue_config(self.group, params={})
        fallback = self.installation.get_link_issue_config(self.group, params={"query": "login"})

        assert "project" not in {field["name"] for field in live}
        assert "project" not in {field["name"] for field in fallback}
        assert not [c for c in responses.calls if "project/list/my" in c.request.url]
