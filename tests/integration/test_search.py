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
SEARCH_RESULT = {
    "content": [{"id": 5, "code": "PLT-5", "title": "Login is broken", "typeRef": {}}],
    "totalPages": 1,
    "pageNumber": 0,
    "totalElements": 1,
}
PROJECTS = {
    "content": [{"id": 1, "title": "Platform", "code": "PLT"}],
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
    def test_it_returns_label_value_pairs_the_frontend_understands(self) -> None:
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.GET, f"{API}/v1/task/searchByTitleCode", json=SEARCH_RESULT)

        with override_settings(ROOT_URLCONF=URLCONF):
            resp = self.client.get(
                self._url(), {"field": "externalIssue", "project": "1", "query": "login"}
            )

        assert resp.status_code == 200
        assert resp.data == [{"label": "PLT-5 — Login is broken", "value": "PLT-5"}]

        # The search really did go to Jaga, scoped to the space the form had selected.
        search = next(c for c in responses.calls if "searchByTitleCode" in c.request.url)
        assert "projectId=1" in search.request.url
        assert "searchText=login" in search.request.url

    @responses.activate
    def test_a_short_query_is_not_an_error_and_does_not_reach_jaga(self) -> None:
        """The frontend fires once with an empty input when the field mounts, and again on the
        first keystroke. Neither is worth an HTTP call to Jaga."""
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.GET, f"{API}/v1/task/searchByTitleCode", json=SEARCH_RESULT)

        with override_settings(ROOT_URLCONF=URLCONF):
            resp = self.client.get(
                self._url(),
                {"field": "externalIssue", "project": "1", "query": "l" * (MIN_QUERY_LENGTH - 1)},
            )

        assert resp.status_code == 200
        assert resp.data == []
        assert not [c for c in responses.calls if "searchByTitleCode" in c.request.url]

    def test_an_unknown_field_is_rejected(self) -> None:
        with override_settings(ROOT_URLCONF=URLCONF):
            resp = self.client.get(self._url(), {"field": "nope", "project": "1", "query": "login"})
        assert resp.status_code == 400

    def test_a_search_without_a_space_is_rejected(self) -> None:
        """Jaga's search endpoint requires a `projectId`; there is no "search everywhere"."""
        with override_settings(ROOT_URLCONF=URLCONF):
            resp = self.client.get(self._url(), {"field": "externalIssue", "query": "login"})
        assert resp.status_code == 400

    def test_an_unknown_integration_is_a_404(self) -> None:
        with override_settings(ROOT_URLCONF=URLCONF):
            url = reverse("sentry-jaga-search", args=[self.organization.slug, self.integration.id])
            bogus = url.replace(f"/{self.integration.id}/", "/123456/")
            resp = self.client.get(bogus, {"field": "externalIssue", "project": "1", "q": "x"})
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
        responses.add(responses.GET, f"{API}/v1/project/list/my", json=PROJECTS, status=200)
        responses.add(responses.GET, f"{API}/v1/task/searchByTitleCode", json=SEARCH_RESULT)

    @responses.activate
    def test_with_the_urlconf_the_task_field_is_a_live_autocomplete(self) -> None:
        self._mock_jaga()

        with override_settings(ROOT_URLCONF=URLCONF):
            config = self.installation.get_link_issue_config(self.group, params={"project": "1"})
        by_name = {field["name"]: field for field in config}

        assert (
            f"/extensions/jaga/search/{self.organization.slug}/{self.integration.id}/"
            == (by_name["externalIssue"]["url"])
        )
        # The `query` box only exists to stand in for autocomplete; with a URL it must be gone.
        assert "query" not in by_name
        # And rendering the form searched nothing — the endpoint does that now.
        assert not [c for c in responses.calls if "searchByTitleCode" in c.request.url]

    @responses.activate
    def test_without_the_urlconf_it_falls_back_to_the_updates_form_search(self) -> None:
        """Sentry's stock urlconf: `reverse` raises, and the form must still work."""
        self._mock_jaga()

        config = self.installation.get_link_issue_config(
            self.group, params={"project": "1", "query": "login"}
        )
        by_name = {field["name"]: field for field in config}

        assert "url" not in by_name["externalIssue"]
        assert by_name["query"]["updatesForm"] is True
        assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]
