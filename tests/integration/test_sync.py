import json

import pytest

pytest.importorskip("sentry")

import responses
from django.core.cache import cache
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.integrations.models.organization_integration import OrganizationIntegration
from sentry.silo.base import SiloMode
from sentry.testutils.cases import APITestCase
from sentry.testutils.silo import assume_test_silo_mode

from sentry_jaga.issue_config import CATEGORY_DONE, CATEGORY_IN_PROGRESS, CATEGORY_TODO

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

SPACE_ID = 11361
TASK_ID = 500

# The statuses of a real space, and the ids Jaga gave them. Names kept verbatim.
TODO_ID = 107391
IN_PROGRESS_ID = 107389
DONE_ID = 107390
SPACE_STATUSES = [
    {"id": TODO_ID, "name": "Сделать", "categoryNameM": CATEGORY_TODO},
    {"id": IN_PROGRESS_ID, "name": "In progress", "categoryNameM": CATEGORY_IN_PROGRESS},
    {"id": DONE_ID, "name": "Готово", "categoryNameM": CATEGORY_DONE},
]


def _raw_task(transitions: list[int] | None = None) -> dict[str, object]:
    """The task as Jaga returns it: the space is an attribute, and the reachable statuses come
    with it. Everything the sync needs is here — nothing is stored on the Sentry side."""
    return {
        "id": TASK_ID,
        "code": "PLT-500",
        "orderNum": 0,
        "statusModifierId": 1,
        "updateTs": "2026-06-25T10:00:00Z",
        "status": {"id": TODO_ID, "name": "Сделать"},
        "statusTransitions": [IN_PROGRESS_ID, DONE_ID] if transitions is None else transitions,
        "executors": [],
        "timeInStatus": {},
        "attributes": [
            {"fieldId": 90, "value": SPACE_ID, "objectTypeNameM": "task.project_id"},
            {"fieldId": 100, "value": "Login is broken", "objectTypeNameM": "task.task_title"},
        ],
    }


class JagaSyncTest(APITestCase):
    def setUp(self) -> None:
        super().setUp()
        # The client caches the token and the per-space statuses in the Django cache, which
        # outlives a test. Clear it so one test cannot serve another's statuses from cache.
        cache.clear()
        # Control-silo models — see the comment in test_issues.py. `ExternalIssue` below is a
        # region model, so it is created in the default (CELL) mode.
        with assume_test_silo_mode(SiloMode.CONTROL):
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
            metadata={"task_id": TASK_ID},
        )

    def _configure(self, **config: object) -> None:
        """Save an organization config and rebuild the installation that reads it."""
        with assume_test_silo_mode(SiloMode.CONTROL):
            org_integration = OrganizationIntegration.objects.get(
                integration_id=self.integration.id, organization_id=self.organization.id
            )
            org_integration.config = config
            org_integration.save()
        self.installation = self.integration.get_installation(self.organization.id)

    def _mock_jaga(self, task: dict[str, object] | None = None) -> None:
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(
            responses.GET,
            f"{API}/v1/task/findExtendedWithFlexField/code/PLT-500",
            json=task if task is not None else _raw_task(),
        )
        responses.add(
            responses.GET, f"{API}/v1/workflowStatusesAvail", json=SPACE_STATUSES, status=200
        )
        responses.add(
            responses.POST, f"{API}/v1/task/updateTaskStatusAndFields", json={}, status=202
        )
        responses.add(responses.POST, f"{API}/v1/comment", json={"id": 1, "taskId": TASK_ID})

    @staticmethod
    def _calls(path: str) -> list[dict[str, object]]:
        return [
            json.loads(call.request.body)
            for call in responses.calls
            if path in call.request.url and call.request.body
        ]

    def test_should_sync_defaults_to_enabled(self) -> None:
        assert self.installation.should_sync("outbound_status") is True

    def test_organization_config_defaults_match_what_the_sync_falls_back_to(self) -> None:
        """The defaults rendered in the form and the fallbacks in `sync_status_outbound` are two
        separate literals that must agree: before the first save the config is empty, and the
        sync still has to behave the way the (unsaved) form claims it will."""
        fields = {field["name"]: field for field in self.installation.get_organization_config()}

        assert set(fields) == {
            "sync_status_forward",
            "resolved_status_category",
            "unresolved_status_category",
            "comment_on_status_change",
        }
        assert fields["sync_status_forward"]["default"] is True
        assert fields["resolved_status_category"]["default"] == CATEGORY_DONE
        assert fields["unresolved_status_category"]["default"] == CATEGORY_TODO
        assert fields["comment_on_status_change"]["default"] is True

    def test_status_category_choices_are_offered_without_calling_jaga(self) -> None:
        """The settings page must render while Jaga is down — an org whose Jaga is unreachable
        still has to be able to open its settings and turn the sync off. Hence the categories
        are constants, not a fetch. `responses` is not active here: any HTTP call would raise."""
        fields = {field["name"]: field for field in self.installation.get_organization_config()}

        for name in ("resolved_status_category", "unresolved_status_category"):
            assert [value for value, _label in fields[name]["choices"]] == [
                CATEGORY_DONE,
                CATEGORY_IN_PROGRESS,
                CATEGORY_TODO,
            ]

    @responses.activate
    def test_sync_status_outbound_moves_the_task_on_resolve(self) -> None:
        """The point of the feature: resolving in Sentry MOVES the Jaga task, and by default
        also comments. The target id is resolved per space — never hardcoded."""
        self._mock_jaga()

        self.installation.sync_status_outbound(
            self.external_issue, is_resolved=True, project_id=self.project.id
        )

        assert self._calls("/v1/task/updateTaskStatusAndFields") == [
            {"taskId": TASK_ID, "targetStatusId": DONE_ID, "formFields": []}
        ]
        [comment] = self._calls("/v1/comment")
        assert "resolved" in str(comment["contentComment"]).lower()

    @responses.activate
    def test_sync_status_outbound_moves_the_task_back_on_regression(self) -> None:
        self._mock_jaga(task=_raw_task(transitions=[TODO_ID, DONE_ID]))

        self.installation.sync_status_outbound(
            self.external_issue, is_resolved=False, project_id=self.project.id
        )

        assert self._calls("/v1/task/updateTaskStatusAndFields") == [
            {"taskId": TASK_ID, "targetStatusId": TODO_ID, "formFields": []}
        ]

    @responses.activate
    def test_sync_status_outbound_honours_the_saved_configuration(self) -> None:
        """The org config actually reaches the core: a different category, and comments off."""
        self._configure(
            sync_status_forward=True,
            resolved_status_category=CATEGORY_IN_PROGRESS,
            unresolved_status_category=CATEGORY_TODO,
            comment_on_status_change=False,
        )
        self._mock_jaga()

        self.installation.sync_status_outbound(
            self.external_issue, is_resolved=True, project_id=self.project.id
        )

        assert self._calls("/v1/task/updateTaskStatusAndFields") == [
            {"taskId": TASK_ID, "targetStatusId": IN_PROGRESS_ID, "formFields": []}
        ]
        assert self._calls("/v1/comment") == []

    @responses.activate
    def test_sync_status_outbound_comments_when_the_task_cannot_be_moved(self) -> None:
        """The workflow offers no step into "done" from here. The task stays put and the comment
        is posted anyway — even though comments are switched off."""
        self._configure(comment_on_status_change=False)
        self._mock_jaga(task=_raw_task(transitions=[IN_PROGRESS_ID]))

        self.installation.sync_status_outbound(
            self.external_issue, is_resolved=True, project_id=self.project.id
        )

        assert self._calls("/v1/task/updateTaskStatusAndFields") == []
        assert len(self._calls("/v1/comment")) == 1

    @responses.activate
    def test_sync_status_outbound_swallows_a_jaga_failure(self) -> None:
        """Jaga being down must not break resolving an issue in Sentry."""
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(
            responses.GET,
            f"{API}/v1/task/findExtendedWithFlexField/code/PLT-500",
            json={"message": "boom"},
            status=500,
        )

        self.installation.sync_status_outbound(
            self.external_issue, is_resolved=True, project_id=self.project.id
        )

        assert self._calls("/v1/task/updateTaskStatusAndFields") == []
