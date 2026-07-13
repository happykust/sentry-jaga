import pytest

pytest.importorskip("sentry")

import responses
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.testutils.cases import APITestCase

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


class JagaSyncTest(APITestCase):
    def setUp(self) -> None:
        super().setUp()
        self.integration = self.create_provider_integration(
            provider="jaga",
            name="Jaga",
            external_id=BASE,
            metadata={"instance_url": BASE, "email": "bot@example.com", "password": "secret"},
        )
        self.integration.add_organization(self.organization, self.user)
        self.installation = self.integration.get_installation(self.organization.id)
        self.external_issue = ExternalIssue.objects.create(
            organization_id=self.organization.id,
            integration_id=self.integration.id,
            key="PLT-500",
            title="Login is broken",
            metadata={"task_id": 500},
        )

    def test_should_sync_defaults_to_enabled(self) -> None:
        assert self.installation.should_sync("outbound_status") is True

    def test_organization_config_exposes_sync_toggle_enabled_by_default(self) -> None:
        """The field default must match the default of `should_sync` — otherwise the UI shows
        an unchecked box while the sync is on, and the very first "Save" kills it."""
        fields = {field["name"]: field for field in self.installation.get_organization_config()}

        assert set(fields) == {"sync_status_forward"}
        assert fields["sync_status_forward"]["default"] is True
        assert self.installation.should_sync("outbound_status") is True

    @responses.activate
    def test_sync_status_outbound_comments_on_resolve(self) -> None:
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.POST, f"{API}/v1/comment", json={"id": 1, "taskId": 500})

        self.installation.sync_status_outbound(
            self.external_issue, is_resolved=True, project_id=self.project.id
        )

        import json

        sent = json.loads(responses.calls[-1].request.body)
        assert sent["taskId"] == 500
        assert "resolved" in sent["contentComment"].lower()

    @responses.activate
    def test_sync_status_outbound_comments_on_regression(self) -> None:
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.POST, f"{API}/v1/comment", json={"id": 2, "taskId": 500})

        self.installation.sync_status_outbound(
            self.external_issue, is_resolved=False, project_id=self.project.id
        )

        import json

        sent = json.loads(responses.calls[-1].request.body)
        assert "reopened" in sent["contentComment"].lower()

    @responses.activate
    def test_sync_status_outbound_resolves_task_id_by_code_when_missing(self) -> None:
        self.external_issue.metadata = {}
        self.external_issue.save()

        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(
            responses.GET,
            f"{API}/v1/task/findExtendedWithFlexField/code/PLT-500",
            json={
                "id": 500,
                "code": "PLT-500",
                "orderNum": 0,
                "statusModifierId": 1,
                "updateTs": "2026-06-25T10:00:00Z",
                "statusTransitions": [],
                "executors": [],
                "timeInStatus": {},
                "status": {"name": "In progress", "nameM": "in_progress"},
                "attributes": [],
            },
        )
        responses.add(responses.POST, f"{API}/v1/comment", json={"id": 3, "taskId": 500})

        self.installation.sync_status_outbound(
            self.external_issue, is_resolved=True, project_id=self.project.id
        )

        import json

        sent = json.loads(responses.calls[-1].request.body)
        assert sent["taskId"] == 500
