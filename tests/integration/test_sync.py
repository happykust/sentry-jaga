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
            name="Яга",
            external_id=BASE,
            metadata={"instance_url": BASE, "email": "bot@example.com", "password": "secret"},
        )
        self.integration.add_organization(self.organization, self.user)
        self.installation = self.integration.get_installation(self.organization.id)
        self.external_issue = ExternalIssue.objects.create(
            organization_id=self.organization.id,
            integration_id=self.integration.id,
            key="PLT-500",
            title="Падает логин",
            metadata={"task_id": 500},
        )

    def test_should_sync_defaults_to_enabled(self) -> None:
        assert self.installation.should_sync("outbound_status") is True

    def test_organization_config_exposes_toggles(self) -> None:
        names = {field["name"] for field in self.installation.get_organization_config()}
        assert {"sync_status_forward", "comment_on_resolve"} <= names

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
        assert "закрыт" in sent["contentComment"].lower()

    @responses.activate
    def test_sync_status_outbound_comments_on_regression(self) -> None:
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.POST, f"{API}/v1/comment", json={"id": 2, "taskId": 500})

        self.installation.sync_status_outbound(
            self.external_issue, is_resolved=False, project_id=self.project.id
        )

        import json

        sent = json.loads(responses.calls[-1].request.body)
        assert "переоткрыт" in sent["contentComment"].lower()

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
                "status": {"name": "В работе", "nameM": "in_progress"},
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
