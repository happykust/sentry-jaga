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
            # The assignee is an attribute like any other, and the cell carries its own `fieldId`
            # — which is what spares the sync a second round trip to the task type.
            {"fieldId": ASSIGNEE_FIELD_ID, "value": [], "objectTypeNameM": "task.assignee_uuid"},
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
            # The author of the notes below. `User` is a control-silo model, so naming it has to
            # happen here — and it has to be named at all, or the attribution assertions would
            # only be comparing the code against itself.
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
        """The defaults rendered in the form and the fallbacks in `sync_status_outbound` are two
        separate literals that must agree: before the first save the config is empty, and the
        sync still has to behave the way the (unsaved) form claims it will."""
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
        # An empty box is the off switch, so this default cannot be sourced from Jaga either —
        # and it must be the same literal the create falls back to (see `issues._auto_label`).
        assert fields["auto_label"]["default"] == DEFAULT_AUTO_LABEL
        # Off by default: an event carries personal data, and the Jaga task may have a wider
        # audience than the Sentry issue.
        assert fields["attach_event"]["default"] is False

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

    # --- Sentry notes -> Jaga comments ------------------------------------------------------
    #
    # Driven by Sentry's `create_comment` / `update_comment` background tasks, which hand the
    # installation `external_issue.key` (the task CODE, not a database id), the id of the user who
    # wrote the note, and the note itself.

    def _note(self, text: str, external_id: object | None = None) -> Activity:
        """A Sentry note, exactly as the background tasks load it: an `Activity` of type NOTE,
        whose text lives in `data["text"]` and whose synced comment id lives in
        `data["external_id"]`."""
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
        """Unlike the status sync, this one defaults OFF — as it does in every issue integration
        upstream. A Sentry note is internal discussion and can name a customer or a credential;
        forwarding it to a tracker with a different audience is a decision, not a surprise."""
        assert self.installation.should_sync("comment") is False

        self._configure(sync_comments=True)
        assert self.installation.should_sync("comment") is True

        self._configure(sync_comments=False)
        assert self.installation.should_sync("comment") is False

    def test_the_comment_checkbox_is_offered_and_its_default_matches_should_sync(self) -> None:
        """The rendered default and the fallback in `should_sync` are two separate literals that
        must agree: before the first save the config is empty, and a checkbox that reads "off"
        while the sync is running (or the other way round) is a lie the first Save makes true."""
        fields = {field["name"]: field for field in self.installation.get_organization_config()}

        assert fields["sync_comments"]["default"] is False
        assert self.installation.should_sync("comment") is False

    def test_the_inbound_syncs_can_never_be_switched_on(self) -> None:
        """Jaga -> Sentry needs webhooks, which this version does not have. A True here would have
        Sentry queue work that silently does nothing."""
        for attribute in ("inbound_assignee", "inbound_status"):
            assert self.installation.should_sync(attribute) is False

    def test_assignee_sync_is_off_until_an_admin_asks_for_it(self) -> None:
        """It puts a named human on a ticket in another system, and notifies them. The rendered
        default and the `should_sync` fallback are separate literals that have to agree."""
        fields = {field["name"]: field for field in self.installation.get_organization_config()}

        assert fields["sync_assignee_forward"]["default"] is False
        assert self.installation.should_sync("outbound_assignee") is False

        self._configure(sync_assignee_forward=True)
        assert self.installation.should_sync("outbound_assignee") is True

    @responses.activate
    def test_sync_assignee_outbound_puts_the_sentry_assignee_on_the_task(self) -> None:
        """Sentry and Jaga are matched by email; the UUID behind it is what Jaga stores.

        The address asserted here is the RpcUser's own — NOT one this test planted. Overwriting
        `User.email` would change what the assertion reads without changing what `RpcUser.emails`
        (which is built from the `UserEmail` table) contains: the test would go green while
        checking nothing.

        The PRIMARY address is the one tried first, and the lookup stops there. That order is
        deliberate: `RpcUser.emails` holds only *verified* addresses, and on a self-hosted Sentry
        with no SMTP nobody has any.
        """
        self._mock_jaga()
        rpc_user = user_service.get_user(user_id=self.user.id)
        assert rpc_user is not None
        # Jaga must answer with the very address it was asked about: the client rejects a profile
        # whose `mail` is somebody else's, because `findByMailOrName` is a fuzzy search and a
        # near-miss would put the wrong human on a real task.
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
        """`user=None` is Sentry's way of saying "nobody", in as many words: its own task resolves
        the user with `user_service.get_user(user_id) if user_id else None`, under the comment
        "Assume unassign if None" (`integrations/tasks/sync_assignee_outbound.py`).

        (An issue assigned to a *team* never reaches here at all — `models/groupassignee.py` only
        queues the outbound sync when `assignee_type == "user"`.)
        """
        self._mock_jaga()

        self.installation.sync_assignee_outbound(self.external_issue, None, assign=True)

        assert self._calls(f"/v1/task/{TASK_ID}")[0]["value"] == []

    @responses.activate
    def test_a_user_with_no_verified_email_must_not_wipe_the_assignee(self) -> None:
        """THE destructive one. `RpcUser.emails` holds only the user's VERIFIED addresses, and
        `UserEmail.is_verified` defaults to False — so on a self-hosted Sentry with no working
        SMTP that set is empty for everybody.

        Reading "no addresses" as "unassign" would mean that switching this sync on turns every
        assignment in Sentry into the REMOVAL of the executor from the linked Jaga task. It has to
        be a lookup miss instead: the task is left exactly as it was, and Sentry is told the target
        could not be found.
        """
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
        """`IntegrationSyncTargetNotFound` is what Sentry's task catches and records as a halt (it
        does not retry it). Silently returning would record the assignment as a success that never
        happened."""
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
    @responses.activate
    def test_jaga_being_down_raises_so_that_sentry_retries_the_assignment(self) -> None:
        """Unlike the status sync, this one does NOT swallow. Sentry's task retries five times, and
        swallowing would throw that retry away AND record the assignment as a success that never
        happened (`ProjectManagementEvent(OUTBOUND_ASSIGNMENT_SYNC).capture()` wraps the call).

        The old version of this test had no assertions at all: it would have passed against a
        `sync_assignee_outbound` that did nothing whatsoever.
        """
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
        # The created comment must come back: `sentry.integrations.tasks.create_comment` reads its
        # id out of the return value and stores it on the note as `external_id`. Without it, an
        # edit of the note later has no comment to point at.
        assert self.installation.get_comment_id(comment) == 1

    @responses.activate
    def test_create_comment_resolves_the_task_code_to_an_id(self) -> None:
        """Sentry hands us `external_issue.key` — the task CODE. Jaga's comment API wants the
        numeric id, and nothing on the Sentry side is given to us to look it up with."""
        self._mock_jaga()

        self.installation.create_comment("PLT-500", self.user.id, self._note("hi"))

        assert [
            c for c in responses.calls if "findExtendedWithFlexField/code/PLT-500" in c.request.url
        ]
        assert self._calls("/v1/comment")[0]["taskId"] == TASK_ID

    @responses.activate
    def test_update_comment_rewrites_the_comment_the_note_created(self) -> None:
        """An edited note must amend its Jaga comment, not append a second one — which is why
        `create_comment` had to return the comment in the first place."""
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
