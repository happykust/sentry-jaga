import json

import pytest

pytest.importorskip("sentry")

import responses
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.silo.base import SiloMode
from sentry.testutils.cases import APITestCase
from sentry.testutils.helpers.datetime import before_now
from sentry.testutils.silo import assume_test_silo_mode
from sentry.testutils.skips import requires_snuba

from sentry_jaga.issue_config import GROUP_ID_FIELD

# The create form reads the issue's latest event (to pre-fill the description, and — with the
# toggle on — to attach it), and an event comes from Snuba.
pytestmark = [requires_snuba]

# The IP address of the user the event was captured from — the thing `sentry:scrub_ip_address`
# exists to keep out of Sentry, and therefore out of Jaga.
CUSTOMER_IP = "203.0.113.7"
# A field an admin has named in `sentry:sensitive_fields`, and one nobody named. Both are
# deliberately meaningless to Sentry's DEFAULT rules (which already catch `password`, `token`,
# `authorization` and friends): only a custom setting can tell the two apart, so a test built on
# them proves that the custom setting is honoured — not that the defaults happen to fire.
SECRET_FIELD = "internal_customer_ref"
KEPT_FIELD = "widget_count"

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
# The attributes of a real Jaga task type: the space and the type are declared as required
# attributes even though both ids are already in the create URL, and Jaga refuses a create
# that leaves them out of `attributes`.
ATTRS_RESPONSE = {
    "id": 10,
    "typeName": "Bug",
    "modulesEnabled": [],
    "groups": [
        {
            "title": "General",
            "orderNum": 0,
            "attributes": [
                {
                    "id": 90,
                    "name": "Space",
                    "objectTypeNameM": "task.project_id",
                    "required": True,
                },
                {
                    "id": 91,
                    "name": "Task type",
                    "objectTypeNameM": "task.type_id",
                    "required": True,
                },
                {
                    "id": 100,
                    "name": "Title",
                    "objectTypeNameM": "task.task_title",
                    "required": True,
                },
                {"id": 101, "name": "Description", "objectTypeNameM": "task.content"},
                {
                    "id": 103,
                    "name": "Assignees",
                    "objectTypeNameM": "task.assignee_uuid",
                    "multiple": True,
                },
                {"id": 104, "name": "Label", "objectTypeNameM": "task.label_id", "multiple": True},
                # A reference with no dictionary behind it: the form leaves it out.
                {"id": 102, "name": "Priority", "objectTypeNameM": "task.priority_id"},
                {"id": 92, "name": "Author", "objectTypeNameM": "task.creator_id"},
                {
                    "id": 110,
                    "name": "Severity",
                    "objectTypeNameM": "task.flex_severity",
                    "dictionaryId": 55,
                },
            ],
        }
    ],
}
# The members of a space come from the user-role matrix, NOT from the documented
# `getUserProfileDtos` — that one answers 200 with `[]` for every space on a live instance, which
# is what used to leave the assignee select silently empty. The matrix carries no person UUID, so
# the select's value is the email and the UUID is resolved at submit time (`PERSON_RESPONSE`).
MATRIX_RESPONSE = {
    "content": [
        {
            "rolesList": [],
            "usersRoles": [
                {
                    "user": {
                        # The TEAM id, despite the name. The Core id is a different number.
                        "id": 365474,
                        "displayName": "Ivanov Ivan",
                        "email": "ivanov@example.com",
                        "isGroup": False,
                        "type": "USER",
                    },
                    "roles": [],
                }
            ],
        }
    ],
    "totalPages": 1,
    "pageNumber": 0,
    "totalElements": 1,
}
PERSON_RESPONSE = {
    "coreId": 193688,
    "teamId": 365474,
    "uuid": "uuid-1",
    "mail": "ivanov@example.com",
    "fullName": "Ivanov Ivan",
}
LABELS_RESPONSE = {
    "content": [{"id": 7, "uuid": "u7", "color": "#fff", "name": "backend", "projects": []}],
    "totalPages": 1,
    "pageNumber": 0,
    "totalElements": 1,
}
# `POST /v1/labels/list` is a get-or-create: this is what a live instance answered for "sentry".
AUTO_LABEL_RESPONSE = {
    "labels": [
        {"id": 17834, "uuid": "u17834", "color": "#8348FC1F", "name": "sentry", "projects": []}
    ]
}
CREATED_TASK = {
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
}


class JagaIssuesTest(APITestCase):
    def setUp(self) -> None:
        super().setUp()
        # `Integration`/`OrganizationIntegration` are control-silo models; the test runs in the
        # default CELL mode, so writing them needs the control silo explicitly. The installation
        # itself is then used from the region silo — which is exactly where Sentry calls it from
        # in production (the issue-linking endpoints live on the region silo).
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

    @staticmethod
    def _mock_attributes() -> None:
        responses.add(responses.GET, f"{API}/v1/project/1/taskType/10", json=ATTRS_RESPONSE)
        responses.add(
            responses.GET,
            f"{API}/v1/listRef/55/any",
            json={
                "name": "Severity",
                "itemsMap": [],
                "items": [{"id": 1, "value": "High", "orderNum": 0}],
            },
        )
        responses.add(
            responses.GET,
            f"{API}/v1/team/userRoles/applications/JAGA/projects/1",
            json=MATRIX_RESPONSE,
        )
        responses.add(
            responses.POST, f"{API}/v1/team/userProfile/findByMailOrName", json=PERSON_RESPONSE
        )
        responses.add(responses.POST, f"{API}/v1/labels/getPage", json=LABELS_RESPONSE)

    @responses.activate
    def test_create_config_lists_projects_and_updates_form(self) -> None:
        self._mock_base()
        responses.add(
            responses.GET, f"{API}/v1/project/1/taskType", json=[{"id": 10, "typeName": "Bug"}]
        )
        self._mock_attributes()

        config = self.installation.get_create_issue_config(self.group, self.user)
        by_name = {field["name"]: field for field in config}

        assert by_name["project"]["updatesForm"] is True
        assert by_name["project"]["choices"] == [("1", "Platform (PLT)")]
        assert by_name["issue_type"]["updatesForm"] is True
        assert by_name["issue_type"]["choices"] == [("10", "Bug")]
        assert by_name["title"]["default"] == "Login is broken"
        assert "Sentry issue:" in by_name["description"]["default"]
        assert by_name["attr_110"]["choices"] == [("1", "High")]
        # The email, not the UUID: see `MATRIX_RESPONSE`.
        assert by_name["attr_103"]["choices"] == [("ivanov@example.com", "Ivanov Ivan")]
        assert by_name["attr_104"]["choices"] == [("7", "backend")]

        # The cascade selects already ask for the space and the type; the author is Jaga's to
        # fill; priority has no value source. None of them belong in the form.
        for hidden in ("attr_90", "attr_91", "attr_92", "attr_102"):
            assert hidden not in by_name

    @staticmethod
    def _mock_create() -> None:
        responses.add(responses.POST, f"{API}/v1/labels/list", json=AUTO_LABEL_RESPONSE)
        responses.add(responses.POST, f"{API}/v1/task/createByTaskType/1/10", json=CREATED_TASK)

    @staticmethod
    def _created_cells() -> dict[int, dict]:
        create = next(c for c in responses.calls if "createByTaskType" in c.request.url)
        return {item["fieldId"]: item for item in json.loads(create.request.body)["attributes"]}

    @responses.activate
    def test_create_issue_posts_attributes_and_returns_key(self) -> None:
        self._mock_base()
        self._mock_attributes()
        self._mock_create()

        result = self.installation.create_issue(
            {
                "project": "1",
                "issue_type": "10",
                "title": "Login is broken",
                "description": "body",
                "attr_110": "1",
            }
        )
        assert result["key"] == "PLT-500"
        assert result["title"] == "Login is broken"

        by_field = self._created_cells()

        # 104 is the label every task from Sentry carries (see the auto-label tests below).
        assert sorted(by_field) == [90, 91, 100, 101, 104, 110]
        # Jaga answers 500 without these two, even though both ids are in the URL.
        assert by_field[90] == {
            "fieldId": 90,
            "value": 1,
            "referenceValue": True,
            "addInfo": {},
        }
        assert by_field[91] == {
            "fieldId": 91,
            "value": 10,
            "referenceValue": True,
            "addInfo": {},
        }

    # --- carrying the Sentry issue through the form ------------------------------------------

    @responses.activate
    def test_the_create_form_carries_the_issue_in_a_hidden_field(self) -> None:
        """`create_issue` is handed the submitted form and nothing else — no group, no event.
        The hidden field is the only way the issue can reach it; Sentry's frontend renders it as
        `display: none` and submits its default with the rest of the form."""
        self._mock_base()
        responses.add(
            responses.GET, f"{API}/v1/project/1/taskType", json=[{"id": 10, "typeName": "Bug"}]
        )
        self._mock_attributes()

        config = self.installation.get_create_issue_config(self.group, self.user)
        by_name = {field["name"]: field for field in config}

        assert by_name[GROUP_ID_FIELD] == {
            "name": GROUP_ID_FIELD,
            "type": "hidden",
            "default": str(self.group.id),
        }

    @responses.activate
    def test_the_form_an_alert_rule_saves_carries_no_issue(self) -> None:
        """The guard against a stale event, and the reason the attachment cannot work for alert
        rules.

        This is the exact call the ticket-rule modal makes — `get_create_issue_config(None, user)`
        (see `IntegrationSerializer`: 'Query param "action" only attached in TicketRuleForm
        modal') — and whatever comes back is SAVED INTO THE RULE. A group id in there would be
        frozen at the moment the rule was written, and every task the rule ever filed afterwards
        would carry the event of that one long-dead issue. So: no group, no field.
        """
        self._mock_base()
        responses.add(
            responses.GET, f"{API}/v1/project/1/taskType", json=[{"id": 10, "typeName": "Bug"}]
        )
        self._mock_attributes()

        config = self.installation.get_create_issue_config(None, self.user)

        assert GROUP_ID_FIELD not in {field["name"] for field in config}

    # --- the label every task filed from Sentry carries -------------------------------------
    #
    # The name comes from the organization's config, which only the Sentry layer can read; the
    # merge itself is the core's, and the unit tests own it. What these prove is the wiring: the
    # default before anything was ever saved, and the empty string as the off switch.

    @responses.activate
    def test_a_task_is_labelled_before_the_organization_configures_anything(self) -> None:
        """`config` is {} until an admin opens the settings page and saves. The label must be on
        anyway — and on the *same* name the settings page renders as its default, or the first
        Save would silently change the behaviour without anybody touching the box."""
        self._mock_base()
        self._mock_attributes()
        self._mock_create()

        assert self.installation.org_integration.config == {}
        self.installation.create_issue(
            {"project": "1", "issue_type": "10", "title": "Login is broken"}
        )

        resolved = next(c for c in responses.calls if c.request.url.endswith("/v1/labels/list"))
        assert json.loads(resolved.request.body) == {"names": ["sentry"]}
        assert self._created_cells()[104]["value"] == ["17834"]

        fields = {f["name"]: f for f in self.installation.get_organization_config()}
        assert fields["auto_label"]["default"] == "sentry"

    @responses.activate
    def test_the_auto_label_joins_the_labels_chosen_in_the_form(self) -> None:
        self._mock_base()
        self._mock_attributes()
        self._mock_create()

        self.installation.create_issue(
            {"project": "1", "issue_type": "10", "title": "t", "attr_104": ["7"]}
        )

        assert self._created_cells()[104]["value"] == ["7", "17834"]

    @responses.activate
    def test_an_organization_that_cleared_the_setting_files_an_unlabelled_task(self) -> None:
        self._mock_base()
        self._mock_attributes()
        self._mock_create()

        self.installation.update_organization_config({"auto_label": ""})
        self.installation.create_issue({"project": "1", "issue_type": "10", "title": "t"})

        assert 104 not in self._created_cells()
        # And Jaga was never asked to make a label.
        assert not [c for c in responses.calls if c.request.url.endswith("/v1/labels/list")]

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
                        "objectTypeNameM": "task.task_title",
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
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(
            responses.POST,
            f"{API}/v1/globalSearch/findTaskList",
            json={
                "content": [
                    {
                        "id": 5,
                        "code": "PLT-5",
                        # The live instance returns the space of a found task as null.
                        "projectId": None,
                        "attributes": [
                            {
                                "fieldId": 100,
                                "value": "Login is broken",
                                "objectTypeNameM": "task.task_title",
                            }
                        ],
                    }
                ],
                "totalPages": 1,
                "pageNumber": 0,
                "totalElements": 1,
            },
        )
        config = self.installation.get_link_issue_config(self.group, params={"query": "login"})
        by_name = {field["name"]: field for field in config}

        assert by_name["query"]["updatesForm"] is True
        assert by_name["externalIssue"]["choices"] == [("PLT-5", "PLT-5 — Login is broken")]
        # No space to pick, and no list of spaces to fetch in order to offer one.
        assert "project" not in by_name

    def test_issue_url(self) -> None:
        assert self.installation.get_issue_url("PLT-500") == f"{BASE}/browse/PLT-500"

    # --- remembering the last space and task type ------------------------------------------
    #
    # This is the half of the feature that only a real Sentry can prove: `store_issue_last_defaults`
    # is Sentry's, it writes to `OrganizationIntegration.config` through a control-silo RPC, and
    # `get_defaults` reads it back. The unit tests cover what the core does with the values; these
    # cover that the values make the round trip at all.

    @staticmethod
    def _mock_two_spaces() -> None:
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(
            responses.GET,
            f"{API}/v1/project/list/my",
            json={
                "content": [
                    {"id": 1, "title": "Platform", "code": "PLT"},
                    {"id": 2, "title": "Billing", "code": "BIL"},
                ],
                "totalPages": 1,
                "pageNumber": 0,
                "totalElements": 2,
            },
            status=200,
        )
        responses.add(
            responses.GET, f"{API}/v1/project/1/taskType", json=[{"id": 10, "typeName": "Bug"}]
        )
        responses.add(
            responses.GET, f"{API}/v1/project/2/taskType", json=[{"id": 20, "typeName": "Incident"}]
        )
        for project_id, type_id in ((1, 10), (2, 20)):
            responses.add(
                responses.GET,
                f"{API}/v1/project/{project_id}/taskType/{type_id}",
                json={"id": type_id, "typeName": "Bug", "modulesEnabled": [], "groups": []},
            )

    def test_persisted_fields_are_the_cascade_fields(self) -> None:
        assert list(self.installation.get_persisted_default_config_fields()) == [
            "project",
            "issue_type",
        ]
        # No per-user defaults: every field of our create form describes the task, not the person
        # filing it, so a team wants them to agree rather than to differ per member.
        assert list(self.installation.get_persisted_user_default_config_fields()) == []

    @responses.activate
    def test_the_create_form_reopens_on_the_space_last_filed_into(self) -> None:
        """The feature, end to end through Sentry's own persistence: a team that filed its last
        task into Billing/Incident gets Billing/Incident again — not the first space in the list."""
        self._mock_two_spaces()

        self.installation.store_issue_last_defaults(
            self.project,
            self.user,
            {"project": "2", "issue_type": "20", "title": "Login is broken"},
        )
        config = self.installation.get_create_issue_config(self.group, self.user)
        by_name = {field["name"]: field for field in config}

        assert by_name["project"]["default"] == "2"
        assert by_name["issue_type"]["default"] == "20"

    @responses.activate
    def test_only_the_persisted_fields_are_stored(self) -> None:
        """`store_issue_last_defaults` filters the submitted form by
        `get_persisted_default_config_fields`. The task title of the last issue must not be
        remembered — it belongs to that issue, not to the project."""
        self._mock_two_spaces()

        self.installation.store_issue_last_defaults(
            self.project,
            self.user,
            {"project": "2", "issue_type": "20", "title": "Login is broken"},
        )

        stored = self.installation.org_integration.config["project_issue_defaults"][
            str(self.project.id)
        ]
        assert stored == {"project": "2", "issue_type": "20"}

    @responses.activate
    def test_a_remembered_space_the_account_lost_access_to_does_not_break_the_form(self) -> None:
        """The failure the persistence makes possible: the remembered space is no longer among
        the ones Jaga offers this service account. The form must still open, on a space that
        exists — and it must not ask Jaga about the space that is gone."""
        self._mock_two_spaces()

        self.installation.store_issue_last_defaults(
            self.project, self.user, {"project": "999", "issue_type": "888"}
        )
        config = self.installation.get_create_issue_config(self.group, self.user)
        by_name = {field["name"]: field for field in config}

        assert by_name["project"]["default"] == "1"
        assert by_name["issue_type"]["default"] == "10"
        assert not [c for c in responses.calls if "/v1/project/999/" in c.request.url]

    # --- the comment posted when an existing task is linked --------------------------------

    @responses.activate
    def test_link_config_prefills_a_comment_linking_back_to_sentry(self) -> None:
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        config = self.installation.get_link_issue_config(self.group, params={})
        by_name = {field["name"]: field for field in config}

        default = by_name["comment"]["default"]
        assert default.startswith("Linked to Sentry issue http")
        assert str(self.group.id) in default
        assert by_name["comment"]["required"] is False

    @responses.activate
    def test_after_link_issue_comments_on_the_task(self) -> None:
        """`after_link_issue` is handed the submitted form and nothing else — no group, no URL.
        The Sentry link reaches Jaga because it was baked into the comment field's default at
        render time. The task id comes from `ExternalIssue.metadata`, so the task the endpoint
        has just fetched is not fetched a second time."""
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.POST, f"{API}/v1/comment", json={"id": 42, "taskId": 500})
        external_issue = ExternalIssue.objects.create(
            organization_id=self.organization.id,
            integration_id=self.integration.id,
            key="PLT-500",
            title="Login is broken",
            metadata={"task_id": 500},
        )

        self.installation.after_link_issue(
            external_issue, data={"comment": "Linked to Sentry issue https://sentry.io/i/1/"}
        )

        posted = [c for c in responses.calls if c.request.url.endswith("/v1/comment")]
        assert json.loads(posted[0].request.body) == {
            "taskId": 500,
            "contentComment": "Linked to Sentry issue https://sentry.io/i/1/",
            "attachIsPending": False,
        }
        # The task was never re-fetched: its id was already on the ExternalIssue.
        assert not [c for c in responses.calls if "findExtendedWithFlexField" in c.request.url]

    @responses.activate
    def test_after_link_issue_posts_nothing_when_the_comment_is_cleared(self) -> None:
        """Clearing the comment box is how a user declines the comment — that is why the feature
        needs no organization-wide toggle."""
        external_issue = ExternalIssue.objects.create(
            organization_id=self.organization.id,
            integration_id=self.integration.id,
            key="PLT-500",
            metadata={"task_id": 500},
        )

        self.installation.after_link_issue(external_issue, data={"comment": ""})
        self.installation.after_link_issue(external_issue, data={})
        self.installation.after_link_issue(external_issue)

        # `responses` is active but nothing is registered: any HTTP call at all would raise.
        assert len(responses.calls) == 0

    @responses.activate
    def test_a_failing_comment_does_not_lose_the_link(self) -> None:
        """The endpoint creates the `ExternalIssue` before this runs and the `GroupLink` after it.
        An exception here would therefore not merely lose the comment — it would lose the link the
        user asked for and orphan the `ExternalIssue`. Jaga being down must not cost the link."""
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.POST, f"{API}/v1/comment", json={"message": "boom"}, status=500)
        external_issue = ExternalIssue.objects.create(
            organization_id=self.organization.id,
            integration_id=self.integration.id,
            key="PLT-500",
            metadata={"task_id": 500},
        )

        self.installation.after_link_issue(external_issue, data={"comment": "hi"})


class JagaEventAttachmentTest(APITestCase):
    """Attaching the Sentry event to the task, behind the organization's toggle.

    Only a real Sentry can hold this half up: the event comes out of Snuba and nodestore, and
    `create_issue` has to find the group by an id that travelled through the browser.
    """

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

        self.event = self.store_event(
            data={
                "event_id": "a" * 32,
                "message": "Login is broken",
                "timestamp": before_now(minutes=1).isoformat(),
                # Everything the toggle exists for: an event is full of personal data, and it
                # holds the same IP address in four different places.
                "user": {"email": "customer@example.com", "ip_address": CUSTOMER_IP},
                "request": {
                    "url": "http://app.example.com/login",
                    "method": "POST",
                    "env": {"REMOTE_ADDR": CUSTOMER_IP},
                    "headers": [["X-Forwarded-For", CUSTOMER_IP], ["User-Agent", "curl"]],
                    # `SECRET_FIELD` is what an admin would name in `sentry:sensitive_fields`;
                    # `KEPT_FIELD` is the control — nothing must touch it.
                    "data": {SECRET_FIELD: "s3kr1t", KEPT_FIELD: "hello"},
                },
                "spans": [
                    {
                        "span_id": "aaaaaaaaaaaaaaaa",
                        "trace_id": "b" * 32,
                        "op": "db",
                        "start_timestamp": before_now(minutes=2).timestamp(),
                        "timestamp": before_now(minutes=1).timestamp(),
                        "sentry_tags": {
                            "user.ip": CUSTOMER_IP,
                            "user": f"ip:{CUSTOMER_IP}",
                            # Not an IP address, and so not the scrub's business.
                            "transaction": "GET /login",
                        },
                    }
                ],
            },
            project_id=self.project.id,
        )
        assert self.event.group is not None
        self.group = self.event.group

    @staticmethod
    def _mock_jaga(attachment_status: int = 200) -> None:
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.GET, f"{API}/v1/project/1/taskType/10", json=ATTRS_RESPONSE)
        responses.add(responses.POST, f"{API}/v1/labels/list", json=AUTO_LABEL_RESPONSE)
        responses.add(responses.POST, f"{API}/v1/task/createByTaskType/1/10", json=CREATED_TASK)
        responses.add(
            responses.POST,
            f"{API}/v1/attacher/file/create",
            json={"id": 1901762, "attachName": "sentry-event.json"}
            if attachment_status == 200
            else {"message": "boom"},
            status=attachment_status,
        )

    def _form_data(self, **extra: object) -> dict[str, object]:
        return {
            "project": "1",
            "issue_type": "10",
            "title": "Login is broken",
            GROUP_ID_FIELD: str(self.group.id),
            **extra,
        }

    @staticmethod
    def _uploads() -> list[responses.Call]:
        return [c for c in responses.calls if "attacher/file/create" in c.request.url]

    def _uploaded_event(self) -> dict:
        """The event JSON, unwrapped from the multipart body it was uploaded in."""
        body = bytes(self._uploads()[0].request.body)
        payload = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
        return dict(json.loads(payload))

    def _uploaded_span_tags(self) -> dict:
        spans = self._uploaded_event()["spans"]
        assert len(spans) == 1
        return dict(spans[0]["sentry_tags"])

    @responses.activate
    def test_nothing_is_attached_until_an_admin_turns_it_on(self) -> None:
        """Off by default, and the default has to be the one an untouched `config` behaves as:
        an event is full of personal data, and the Jaga task may have a wider audience than the
        Sentry issue."""
        self._mock_jaga()

        assert self.installation.org_integration.config == {}
        result = self.installation.create_issue(self._form_data())

        assert result["key"] == "PLT-500"
        assert self._uploads() == []

        fields = {f["name"]: f for f in self.installation.get_organization_config()}
        assert fields["attach_event"]["default"] is False

    @responses.activate
    def test_the_event_is_attached_to_the_task_when_the_toggle_is_on(self) -> None:
        self._mock_jaga()
        self.installation.update_organization_config({"attach_event": True})

        self.installation.create_issue(self._form_data())

        upload = self._uploads()[0]
        # Jaga files attachments under a space, and `taskId` is what binds the file to the task.
        assert "projectId=1" in upload.request.url
        assert "taskId=500" in upload.request.url

        body = bytes(upload.request.body)
        assert b'filename="sentry-event-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"' in body
        assert b"Content-Type: application/json" in body
        # The file really is the event, as Sentry itself serves it behind the JSON link on an
        # event page (`EventJsonEndpoint` -> `event.as_dict()`)...
        assert b'"event_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"' in body
        # ...personal data and all. This is precisely why the toggle is off by default.
        assert b"customer@example.com" in body

    # --- the project's privacy settings -----------------------------------------------------
    #
    # The attachment goes through `sentry.relay.datascrubbing.scrub_data` — Sentry's OWN scrubber,
    # the same Relay engine, and the same rules, that clean an event as it comes into Sentry. So
    # the file honours every privacy setting of the project and the organization: the IP scrubbing,
    # the sensitive fields, the default rules, and the PII config an admin wrote by hand. Scrubbing
    # of our own could only ever cover the fields we happened to think of.
    #
    # It runs against the STORED event, which makes it stricter than Sentry's own JSON page for
    # free: Relay only ever cleaned events that arrived *after* a setting was turned on, and the
    # page still shows the addresses in the older ones.
    #
    # Note what the harness does NOT do: `store_event` goes through EventManager, not Relay, so
    # nothing is scrubbed at ingest here whatever the settings say. The fixture therefore always
    # carries the secrets in full — anything missing from the upload was taken out on the way out,
    # and the "nothing configured" test below proves the fixture is not simply empty.

    @responses.activate
    def test_the_ips_are_scrubbed_when_the_project_asks_for_it(self) -> None:
        """Every one of the four places this event carries the customer's address."""
        self._mock_jaga()
        self.installation.update_organization_config({"attach_event": True})
        self.project.update_option("sentry:scrub_ip_address", True)

        self.installation.create_issue(self._form_data())

        event = self._uploaded_event()
        assert event["user"]["ip_address"] is None
        assert event["request"]["env"]["REMOTE_ADDR"] is None
        assert event["request"]["headers"] == [["X-Forwarded-For", "[ip]"], ["User-Agent", "curl"]]
        # Sentry's scrubber NULLS an address field and rewrites an address embedded in a longer
        # string. (`EventJsonEndpoint` deletes the `user.ip` key instead of nulling it — same
        # effect, and the scrubber's own form is the one to follow.)
        assert self._uploaded_span_tags() == {
            "transaction": "GET /login",
            "user": "ip:[ip]",
            "user.ip": None,
        }

        # Not one of them survives anywhere in the file — headers and span tags included.
        assert CUSTOMER_IP.encode() not in bytes(self._uploads()[0].request.body)

        # The setting is about IP addresses and nothing else: the email still travels. That is the
        # whole reason this attachment is off by default.
        assert event["user"]["email"] == "customer@example.com"

    @responses.activate
    def test_the_ips_are_scrubbed_when_the_organization_requires_it(self) -> None:
        """The organization-wide setting overrules the project, and must be honoured too — which
        it is, for free, because the rules come from Sentry's own scrubber."""
        self._mock_jaga()
        self.installation.update_organization_config({"attach_event": True})
        self.organization.update_option("sentry:require_scrub_ip_address", True)

        self.installation.create_issue(self._form_data())

        assert self._uploaded_event()["user"]["ip_address"] is None
        assert CUSTOMER_IP.encode() not in bytes(self._uploads()[0].request.body)

    @responses.activate
    def test_a_sensitive_field_the_admin_named_is_scrubbed(self) -> None:
        """The gap that hand-written scrubbing left wide open, and the reason this now goes through
        Sentry's scrubber: an admin who told Sentry to strip a field must not find it in plain text
        on a Jaga task, in a tracker with a wider audience than the Sentry issue.

        The field is deliberately one Sentry's DEFAULT rules know nothing about, so a green test
        here is the admin's own setting being honoured — not the defaults happening to fire.
        """
        self._mock_jaga()
        self.installation.update_organization_config({"attach_event": True})
        self.project.update_option("sentry:sensitive_fields", [SECRET_FIELD])

        self.installation.create_issue(self._form_data())

        body = self._uploaded_event()["request"]["data"]
        assert body[SECRET_FIELD] == "[Filtered]"
        assert b"s3kr1t" not in bytes(self._uploads()[0].request.body)
        # And only that field: a scrubber that ate the whole request body would "pass" the line
        # above while destroying the point of attaching the event at all.
        assert body[KEPT_FIELD] == "hello"

    @responses.activate
    def test_nothing_is_scrubbed_that_the_project_did_not_ask_for(self) -> None:
        """The control, and the one that makes the three tests above mean something: with nothing
        configured, every secret is in the file — so a green test above is the scrubber working,
        not an empty fixture."""
        self._mock_jaga()
        self.installation.update_organization_config({"attach_event": True})

        self.installation.create_issue(self._form_data())

        event = self._uploaded_event()
        assert event["user"]["ip_address"] == CUSTOMER_IP
        assert event["request"]["env"]["REMOTE_ADDR"] == CUSTOMER_IP
        assert event["request"]["data"][SECRET_FIELD] == "s3kr1t"
        assert event["request"]["data"][KEPT_FIELD] == "hello"
        assert self._uploaded_span_tags() == {
            "transaction": "GET /login",
            "user.ip": CUSTOMER_IP,
            "user": f"ip:{CUSTOMER_IP}",
        }

    @responses.activate
    def test_a_task_filed_by_an_alert_rule_gets_no_attachment(self) -> None:
        """The rule's saved form has no `sentry_group_id` — `get_create_issue_config` emits none
        without a group — so there is no issue to take an event from. That is the honest outcome:
        the alternative would have been a group id frozen into the rule, attaching one stale event
        to every task the rule ever filed."""
        self._mock_jaga()
        self.installation.update_organization_config({"attach_event": True})

        # Exactly what the ticket action hands `create_issue`: the saved cascade plus the title
        # and description of the event that fired the rule. No group.
        result = self.installation.create_issue(
            {"project": "1", "issue_type": "10", "title": "Login is broken", "description": "body"}
        )

        assert result["key"] == "PLT-500"
        assert self._uploads() == []

    @responses.activate
    def test_a_group_of_another_organization_is_not_attached(self) -> None:
        """The group id arrives in a hidden field — that is, from the browser. Unscoped, a
        hand-edited request would pull the latest event of any group on the instance, out of
        another customer's project, and file it onto a Jaga task.

        The other organization's issue is given a real event ON PURPOSE: an issue with no event
        attaches nothing anyway, and a test built on one would pass with the scoping deleted.
        """
        self._mock_jaga()
        self.installation.update_organization_config({"attach_event": True})

        other_project = self.create_project(organization=self.create_organization())
        other_event = self.store_event(
            data={
                "event_id": "b" * 32,
                "message": "Another customer's issue",
                "timestamp": before_now(minutes=1).isoformat(),
                "user": {"email": "someone-elses-customer@example.com"},
            },
            project_id=other_project.id,
        )
        assert other_event.group is not None

        result = self.installation.create_issue(
            self._form_data(**{GROUP_ID_FIELD: str(other_event.group.id)})
        )

        assert result["key"] == "PLT-500"
        assert self._uploads() == []

    @responses.activate
    def test_a_failing_upload_does_not_lose_the_task(self) -> None:
        """The task is already created by the time the upload runs, and the caller is about to
        link it to the Sentry issue. Raising here would fail a create that in fact succeeded —
        and orphan the task in Jaga — over a file nobody may even have noticed was coming."""
        self._mock_jaga(attachment_status=500)
        self.installation.update_organization_config({"attach_event": True})

        result = self.installation.create_issue(self._form_data())

        assert result["key"] == "PLT-500"
        assert len(self._uploads()) == 1
