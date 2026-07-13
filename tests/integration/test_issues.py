import pytest

pytest.importorskip("sentry")

import responses
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
ATTRS_RESPONSE = {
    "id": 10,
    "typeName": "Bug",
    "modulesEnabled": [],
    "groups": [
        {
            "title": "General",
            "orderNum": 0,
            "attributes": [
                {"id": 100, "name": "Title", "objectTypeNameM": "task.title", "required": True},
                {"id": 101, "name": "Description", "objectTypeNameM": "task.content_data"},
                {
                    "id": 102,
                    "name": "Priority",
                    "objectTypeNameM": "task.priority",
                    "dictionaryId": 55,
                },
            ],
        }
    ],
}


class JagaIssuesTest(APITestCase):
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
        self.group = self.create_group(project=self.project, message="Login is broken")

    @staticmethod
    def _mock_base() -> None:
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(
            responses.GET,
            f"{API}/v1/project/list/my",
            json={
                "content": [{"id": 1, "title": "Platform", "code": "PLT"}],
                "totalPages": 1,
                "pageNumber": 0,
                "totalElements": 1,
            },
            status=200,
        )

    @responses.activate
    def test_create_config_lists_projects_and_updates_form(self) -> None:
        self._mock_base()
        responses.add(
            responses.GET, f"{API}/v1/project/1/taskType", json=[{"id": 10, "typeName": "Bug"}]
        )
        responses.add(responses.GET, f"{API}/v1/project/1/taskType/10", json=ATTRS_RESPONSE)
        responses.add(
            responses.GET,
            f"{API}/v1/listRef/55/any",
            json={
                "name": "Priorities",
                "itemsMap": [],
                "items": [{"id": 1, "value": "High", "orderNum": 0}],
            },
        )

        config = self.installation.get_create_issue_config(self.group, self.user)
        by_name = {field["name"]: field for field in config}

        assert by_name["project"]["updatesForm"] is True
        assert by_name["project"]["choices"] == [("1", "Platform (PLT)")]
        assert by_name["issue_type"]["updatesForm"] is True
        assert by_name["issue_type"]["choices"] == [("10", "Bug")]
        assert by_name["attr_100"]["default"] == "Login is broken"
        assert "Sentry issue:" in by_name["attr_101"]["default"]
        assert by_name["attr_102"]["choices"] == [("1", "High")]

    @responses.activate
    def test_create_issue_posts_attributes_and_returns_key(self) -> None:
        self._mock_base()
        responses.add(responses.GET, f"{API}/v1/project/1/taskType/10", json=ATTRS_RESPONSE)
        responses.add(
            responses.POST,
            f"{API}/v1/task/createByTaskType/1/10",
            json={
                "id": 500,
                "code": "PLT-500",
                "orderNum": 0,
                "statusId": 1,
                "statusModifierId": 1,
                "taskTypeId": 10,
                "updateTs": "2026-06-25T10:00:00Z",
                "statusTransitions": [],
                "colorIndicator": [],
                "timeInStatus": {},
                "attributes": [],
            },
        )

        result = self.installation.create_issue(
            {
                "project": "1",
                "issue_type": "10",
                "attr_100": "Login is broken",
                "attr_101": "body",
                "attr_102": "1",
            }
        )
        assert result["key"] == "PLT-500"
        assert result["title"] == "Login is broken"

        import json

        sent = json.loads(responses.calls[-1].request.body)
        field_ids = [item["fieldId"] for item in sent["attributes"]]
        assert field_ids == [100, 101, 102]

    @responses.activate
    def test_get_issue_by_code(self) -> None:
        self._mock_base()
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
                "attributes": [
                    {
                        "fieldId": 100,
                        "value": "Login is broken",
                        "objectTypeNameM": "task.title",
                        "referenceValue": False,
                    }
                ],
            },
        )
        issue = self.installation.get_issue("PLT-500")
        assert issue["key"] == "PLT-500"
        assert issue["title"] == "Login is broken"

    @responses.activate
    def test_link_config_offers_search(self) -> None:
        self._mock_base()
        responses.add(
            responses.GET,
            f"{API}/v1/task/searchByTitleCode",
            json={
                "content": [{"id": 5, "code": "PLT-5", "title": "Login is broken", "typeRef": {}}],
                "totalPages": 1,
                "pageNumber": 0,
                "totalElements": 1,
            },
        )
        config = self.installation.get_link_issue_config(
            self.group, params={"project": "1", "query": "login"}
        )
        by_name = {field["name"]: field for field in config}
        assert by_name["query"]["updatesForm"] is True
        assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]

    def test_issue_url(self) -> None:
        assert self.installation.get_issue_url("PLT-500") == f"{BASE}/task/PLT-500"
