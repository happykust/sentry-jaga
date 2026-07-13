import pytest

pytest.importorskip("sentry")

import responses
from sentry.integrations.models.integration import Integration
from sentry.integrations.models.organization_integration import (
    OrganizationIntegration,
)
from sentry.testutils.cases import IntegrationTestCase

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


class JagaInstallationTest(IntegrationTestCase):
    provider = JagaIntegrationProvider

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
            responses.POST, f"{API}/v1/auth/login", json={"message": "Неверный пароль"}, status=401
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
            name="Яга",
            external_id=BASE,
            metadata={"instance_url": BASE, "email": "bot@example.com", "password": "secret"},
        )
        integration.add_organization(self.organization, self.user)

        installation = integration.get_installation(self.organization.id)
        client = installation.get_client()
        assert client.base_url == API
