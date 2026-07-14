import json

import pytest

pytest.importorskip("sentry")

import responses
from django.core.cache import cache
from sentry.integrations.mixins.issues import IntegrationSyncTargetNotFound
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.integrations.models.organization_integration import OrganizationIntegration
from sentry.models.activity import Activity
from sentry.shared_integrations.exceptions import IntegrationError
from sentry.silo.base import SiloMode
from sentry.testutils.cases import APITestCase
from sentry.testutils.silo import assume_test_silo_mode
from sentry.types.activity import ActivityType
from sentry.users.models.useremail import UserEmail
from sentry.users.services.user.service import user_service

from sentry_jaga.issue_config import (
    CATEGORY_DONE,
    CATEGORY_IN_PROGRESS,
    CATEGORY_TODO,
    DEFAULT_AUTO_LABEL,
)

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
# The id of the `task.assignee_uuid` cell on the task (867868 on the live instance).
ASSIGNEE_FIELD_ID = 867868
PERSON = {
    "coreId": 193688,
    "teamId": 365474,
    "uuid": "aea8739a-c7dc-49c3-b1e5-5bc909ef364f",
    "mail": "ivanov@example.com",
    "fullName": "Ivanov Ivan",
}

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
    """The task as Jaga returns it: everything the sync needs is here, nothing is stored on the
    Sentry side."""
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
            # The cell carries its own `fieldId`, which spares the sync a round trip.
            {"fieldId": ASSIGNEE_FIELD_ID, "value": [], "objectTypeNameM": "task.assignee_uuid"},
        ],
    }


class JagaSyncTest(APITestCase):
    def setUp(self) -> None:
        super().setUp()
        # The Django cache outlives a test: clear it so one test cannot serve another's statuses.
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
            # The author of the notes below must be named, or the attribution assertions would
            # only compare the code against itself.
            self.user.update(name="Ivanov Ivan")

        self.installation = self.integration.get_installation(self.organization.id)
        self.group = self.create_group(project=self.project, message="Login is broken")
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
        responses.add(responses.PATCH, f"{API}/v1/task/{TASK_ID}", json={"id": TASK_ID})

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
        """The rendered defaults and the fallbacks in `sync_status_outbound` are separate literals
        that must agree: before the first save the config is empty, and the sync still has to
        behave the way the unsaved form claims it will."""
        fields = {field["name"]: field for field in self.installation.get_organization_config()}

        assert set(fields) == {
            "sync_status_forward",
            "sync_assignee_forward",
            "resolved_status_category",
            "unresolved_status_category",
            "comment_on_status_change",
            "sync_comments",
            "auto_label",
            "attach_event",
        }
        assert fields["sync_status_forward"]["default"] is True
        assert fields["resolved_status_category"]["default"] == CATEGORY_DONE
        assert fields["unresolved_status_category"]["default"] == CATEGORY_TODO
        assert fields["comment_on_status_change"]["default"] is True
        assert fields["sync_comments"]["default"] is False
        # Must be the same literal the create falls back to (see `issues._auto_label`).
        assert fields["auto_label"]["default"] == DEFAULT_AUTO_LABEL
        # Off by default: an event carries personal data, and the Jaga task may have a wider
        # audience than the Sentry issue.
        assert fields["attach_event"]["default"] is False

    def test_status_category_choices_are_offered_without_calling_jaga(self) -> None:
        """An org whose Jaga is unreachable must still be able to open its settings and turn the
        sync off, so the categories are constants; `responses` is not active, so any HTTP call
        raises."""
        fields = {field["name"]: field for field in self.installation.get_organization_config()}

        for name in ("resolved_status_category", "unresolved_status_category"):
            assert [value for value, _label in fields[name]["choices"]] == [
                CATEGORY_DONE,
                CATEGORY_IN_PROGRESS,
                CATEGORY_TODO,
            ]

    @responses.activate
    def test_sync_status_outbound_moves_the_task_on_resolve(self) -> None:
        """Resolving in Sentry moves the Jaga task and, by default, comments; the target id is
        resolved per space, never hardcoded."""
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
        """The workflow offers no step into "done" from here: the task stays put and the comment is
        posted anyway, even with comments switched off."""
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

    # --- Sentry notes -> Jaga comments ------------------------------------------------------
    #
    # Driven by Sentry's `create_comment` / `update_comment` background tasks, which hand the
    # installation `external_issue.key` (the task CODE, not a database id), the id of the user who
    # wrote the note, and the note itself.

    def _note(self, text: str, external_id: object | None = None) -> Activity:
        """A Sentry note as the background tasks load it: an `Activity` of type NOTE, with its text
        in `data["text"]` and its synced comment id in `data["external_id"]`."""
        data: dict[str, object] = {"text": text}
        if external_id is not None:
            data["external_id"] = external_id
        return Activity.objects.create(
            group=self.group,
            project=self.project,
            type=ActivityType.NOTE.value,
            user_id=self.user.id,
            data=data,
        )

    def test_comment_sync_is_off_until_an_admin_turns_it_on(self) -> None:
        """A Sentry note is internal discussion and can name a customer or a credential, so
        forwarding it to a tracker with a different audience must be a decision, not a surprise."""
        assert self.installation.should_sync("comment") is False

        self._configure(sync_comments=True)
        assert self.installation.should_sync("comment") is True

        self._configure(sync_comments=False)
        assert self.installation.should_sync("comment") is False

    def test_the_comment_checkbox_is_offered_and_its_default_matches_should_sync(self) -> None:
        """The rendered default and the fallback in `should_sync` are separate literals that must
        agree: a checkbox reading "off" while the sync runs is a lie the first Save makes true."""
        fields = {field["name"]: field for field in self.installation.get_organization_config()}

        assert fields["sync_comments"]["default"] is False
        assert self.installation.should_sync("comment") is False

    def test_the_inbound_syncs_can_never_be_switched_on(self) -> None:
        """Jaga -> Sentry needs webhooks, which this version does not have: a True here would have
        Sentry queue work that silently does nothing."""
        for attribute in ("inbound_assignee", "inbound_status"):
            assert self.installation.should_sync(attribute) is False

    def test_assignee_sync_is_off_until_an_admin_asks_for_it(self) -> None:
        """It puts a named human on a ticket in another system and notifies them; the rendered
        default and the `should_sync` fallback are separate literals that have to agree."""
        fields = {field["name"]: field for field in self.installation.get_organization_config()}

        assert fields["sync_assignee_forward"]["default"] is False
        assert self.installation.should_sync("outbound_assignee") is False

        self._configure(sync_assignee_forward=True)
        assert self.installation.should_sync("outbound_assignee") is True

    @responses.activate
    def test_sync_assignee_outbound_puts_the_sentry_assignee_on_the_task(self) -> None:
        """The address asserted here is the RpcUser's OWN, never one this test planted: overwriting
        `User.email` would not change `RpcUser.emails` (built from the `UserEmail` table), and the
        test would go green while checking nothing."""
        self._mock_jaga()
        rpc_user = user_service.get_user(user_id=self.user.id)
        assert rpc_user is not None
        # Jaga must answer with the very address it was asked about: `findByMailOrName` is a fuzzy
        # search, and a near-miss would put the wrong human on a real task.
        responses.add(
            responses.POST,
            f"{API}/v1/team/userProfile/findByMailOrName",
            json={**PERSON, "mail": rpc_user.email},
        )

        self.installation.sync_assignee_outbound(self.external_issue, rpc_user, assign=True)

        assert self._calls("/v1/team/userProfile/findByMailOrName") == [
            {"searchText": rpc_user.email}
        ], "the Sentry user's own primary address is the key the two systems are matched on"
        assert self._calls(f"/v1/task/{TASK_ID}") == [
            {
                "fieldId": ASSIGNEE_FIELD_ID,
                "value": [PERSON["uuid"]],
                "referenceValue": True,
                "addInfo": {},
                "objectTypeNameM": "task.assignee_uuid",
            }
        ]

    @responses.activate
    def test_sync_assignee_outbound_clears_the_task_when_the_issue_is_unassigned(self) -> None:
        """Verified against a live instance: an empty list is how Jaga is told "nobody"."""
        self._mock_jaga()

        self.installation.sync_assignee_outbound(
            self.external_issue, user_service.get_user(user_id=self.user.id), assign=False
        )

        assert self._calls(f"/v1/task/{TASK_ID}")[0]["value"] == []
        assert not [c for c in responses.calls if "findByMailOrName" in c.request.url], (
            "unassigning asks Jaga about nobody"
        )

    @responses.activate
    def test_no_user_means_unassign_which_is_what_sentry_means_by_it(self) -> None:
        """`user=None` is Sentry's way of saying "nobody" ("Assume unassign if None", in
        `integrations/tasks/sync_assignee_outbound.py`); an issue assigned to a TEAM never reaches
        the sync at all."""
        self._mock_jaga()

        self.installation.sync_assignee_outbound(self.external_issue, None, assign=True)

        assert self._calls(f"/v1/task/{TASK_ID}")[0]["value"] == []

    @responses.activate
    def test_a_user_with_no_verified_email_must_not_wipe_the_assignee(self) -> None:
        """`RpcUser.emails` holds only VERIFIED addresses — empty for everybody on an SMTP-less
        self-hosted Sentry — so reading "no addresses" as "unassign" would make every assignment
        WIPE the Jaga assignee; it must be a lookup miss that leaves the task untouched."""
        self._mock_jaga()
        responses.add(
            responses.POST,
            f"{API}/v1/team/userProfile/findByMailOrName",
            json={"error": "NotFoundException: User does not exists"},
            status=400,
        )
        with assume_test_silo_mode(SiloMode.CONTROL):
            UserEmail.objects.filter(user_id=self.user.id).update(is_verified=False)
        rpc_user = user_service.get_user(user_id=self.user.id)
        assert rpc_user is not None
        assert rpc_user.emails == frozenset(), "the premise of this test"

        with pytest.raises(IntegrationSyncTargetNotFound):
            self.installation.sync_assignee_outbound(self.external_issue, rpc_user, assign=True)

        assert self._calls(f"/v1/task/{TASK_ID}") == [], "the task must be left exactly as it was"

    @responses.activate
    def test_a_sentry_user_with_no_jaga_account_is_reported_not_written(self) -> None:
        """Sentry's task catches `IntegrationSyncTargetNotFound` and records a halt, so silently
        returning would record the assignment as a success that never happened."""
        self._mock_jaga()
        responses.add(
            responses.POST,
            f"{API}/v1/team/userProfile/findByMailOrName",
            json={"error": "NotFoundException: User does not exists"},
            status=400,
        )

        with pytest.raises(IntegrationSyncTargetNotFound):
            self.installation.sync_assignee_outbound(
                self.external_issue, user_service.get_user(user_id=self.user.id), assign=True
            )

        assert self._calls(f"/v1/task/{TASK_ID}") == [], "the task keeps whoever it had"

    @responses.activate
    def test_jaga_being_down_raises_so_that_sentry_retries_the_assignment(self) -> None:
        """Unlike the status sync, this one does NOT swallow: Sentry's task retries five times, and
        swallowing would throw that retry away and record a success that never happened."""
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(
            responses.GET, f"{API}/v1/task/findExtendedWithFlexField/code/PLT-500", status=500
        )

        with pytest.raises(IntegrationError):
            self.installation.sync_assignee_outbound(
                self.external_issue, user_service.get_user(user_id=self.user.id), assign=True
            )

        assert self._calls(f"/v1/task/{TASK_ID}") == [], "nothing may be written when Jaga is down"

    @responses.activate
    def test_create_comment_posts_the_note_attributed_to_its_author(self) -> None:
        """The comment is created by the service account, so without the attribution line every
        note in Jaga would look as though the bot had written it."""
        self._mock_jaga()

        comment = self.installation.create_comment(
            "PLT-500", self.user.id, self._note("Looks like a bad deploy")
        )

        [posted] = self._calls("/v1/comment")
        assert posted == {
            "taskId": TASK_ID,
            "contentComment": "Ivanov Ivan wrote:\n\n> Looks like a bad deploy",
            "attachIsPending": False,
        }
        # Sentry stores the returned id on the note as `external_id`; without it, a later edit has
        # no comment to point at.
        assert self.installation.get_comment_id(comment) == 1

    @responses.activate
    def test_create_comment_resolves_the_task_code_to_an_id(self) -> None:
        """Sentry hands us `external_issue.key`, the task CODE, while Jaga's comment API wants the
        numeric id."""
        self._mock_jaga()

        self.installation.create_comment("PLT-500", self.user.id, self._note("hi"))

        assert [
            c for c in responses.calls if "findExtendedWithFlexField/code/PLT-500" in c.request.url
        ]
        assert self._calls("/v1/comment")[0]["taskId"] == TASK_ID

    @responses.activate
    def test_update_comment_rewrites_the_comment_the_note_created(self) -> None:
        """An edited note must amend its Jaga comment, not append a second one — which is why
        `create_comment` returns the comment at all."""
        self._mock_jaga()
        responses.add(responses.PUT, f"{API}/v1/comment", json={"id": 1, "taskId": TASK_ID})

        self.installation.update_comment(
            "PLT-500", self.user.id, self._note("Reworded", external_id=1)
        )

        puts = [c for c in responses.calls if c.request.method == "PUT"]
        assert json.loads(puts[0].request.body) == {
            "id": 1,
            "taskId": TASK_ID,
            "contentComment": "Ivanov Ivan wrote:\n\n> Reworded",
            "attachIsPending": False,
        }
        # Nothing was POSTed: an edit does not create a comment.
        assert [
            c for c in responses.calls if c.request.method == "POST" and "comment" in c.request.url
        ] == []
