import pytest

pytest.importorskip("sentry")

import responses
from sentry.integrations.models.integration import Integration
from sentry.integrations.models.organization_integration import (
    OrganizationIntegration,
)
from sentry.testutils.cases import IntegrationTestCase
from sentry.testutils.silo import control_silo_test

from sentry_jaga.integration import JagaIntegrationProvider

BASE = "https://jaga.example.com"
API = f"{BASE}/external-api"
AUTH_OK = {
    "accessToken": "at",
    "refreshToken": "rt",
    "expiresAt": "2099-01-01T00:00:00Z",
    "id": 1,
    "email": "bot@example.com",
    "fullName": "Bot",
}


# The installation pipeline view (/organizations/<org>/integrations/jaga/setup/) is a
# control-silo view: `Integration` and `OrganizationIntegration` live in the control silo. In
# the default test silo mode (CELL) the request is rejected before it ever reaches the pipeline
# — "Received GET request ... to server in REGION mode. This view is available only in:
# CONTROL, MONOLITH". Sentry marks its own installation tests the same way (see
# BitbucketServerIntegrationTest, the closest analogue: a self-hosted instance with a URL and
# credentials in the form).
@control_silo_test
class JagaInstallationTest(IntegrationTestCase):
    provider = JagaIntegrationProvider

    def test_setup_form_renders_fields_through_crispy(self) -> None:
        """The setup page renders every field, its help text and the service-account notice.

        A bare `assert status_code == 200` would still pass if `{% load crispy_forms_tags %}`
        silently produced nothing — crispy is Sentry's, not ours, and the template only works
        because Sentry ships django-crispy-forms (Jira Server's setup page uses it too).
        """
        resp = self.client.get(self.init_path)
        assert resp.status_code == 200
        html = resp.content.decode()

        for field in ("instance_url", "email", "password"):
            assert f'name="{field}"' in html, f"{field} was not rendered"
        # A help text from the form and the alert block from the template.
        assert "Sentry appends the API prefix itself" in html
        assert "service account" in html

    @responses.activate
    def test_installation_creates_integration(self) -> None:
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)

        resp = self.client.get(self.init_path)
        assert resp.status_code == 200

        resp = self.client.post(
            self.init_path,
            data={
                "instance_url": BASE,
                "email": "bot@example.com",
                "password": "secret",
            },
        )
        assert resp.status_code == 200
        self.assertDialogSuccess(resp)

        integration = Integration.objects.get(provider="jaga")
        assert integration.external_id == BASE
        assert integration.metadata["instance_url"] == BASE
        assert integration.metadata["email"] == "bot@example.com"
        assert integration.metadata["password"] == "secret"

        assert OrganizationIntegration.objects.filter(
            integration_id=integration.id, organization_id=self.organization.id
        ).exists()

    @responses.activate
    def test_installation_rejects_bad_credentials(self) -> None:
        responses.add(
            responses.POST,
            f"{API}/v1/auth/login",
            json={"message": "Invalid password"},
            status=401,
        )

        resp = self.client.post(
            self.init_path,
            data={"instance_url": BASE, "email": "bot@example.com", "password": "wrong"},
        )
        assert resp.status_code == 200
        assert not Integration.objects.filter(provider="jaga").exists()

    @responses.activate
    def test_get_client_uses_stored_metadata(self) -> None:
        integration = self.create_provider_integration(
            provider="jaga",
            name="Jaga",
            external_id=BASE,
            metadata={"instance_url": BASE, "email": "bot@example.com", "password": "secret"},
        )
        integration.add_organization(self.organization, self.user)

        installation = integration.get_installation(self.organization.id)
        client = installation.get_client()
        assert client.base_url == API
