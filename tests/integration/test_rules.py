"""The alert-rule action against a real Sentry: the wiring, not the Jaga call.

The registry is built during `django.setup()`, before any plugin is registered, so the documented
`Plugin2.get_rules()` door is too late and the action is registered from `JagaAppConfig.ready()`
instead. These tests cover that it lands there, that Sentry's rule API accepts a rule using it,
and that firing the rule files a Jaga task and links it to the group.
"""

import json

import pytest

pytest.importorskip("sentry")

import responses
from django.urls import reverse
from rest_framework.test import APITestCase as BaseAPITestCase
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.models.grouplink import GroupLink
from sentry.models.rule import Rule
from sentry.rules import rules
from sentry.silo.base import SiloMode
from sentry.testutils.cases import RuleTestCase
from sentry.testutils.silo import assume_test_silo_mode
from sentry.testutils.skips import requires_snuba
from sentry.types.rules import RuleFuture

from sentry_jaga.notify_action import JagaCreateTicketAction

pytestmark = [requires_snuba]

BASE = "https://jaga.example.com"
API = f"{BASE}/external-api"
ACTION_ID = "sentry_jaga.notify_action.JagaCreateTicketAction"

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
                {"id": 90, "name": "Space", "objectTypeNameM": "task.project_id", "required": True},
                {
                    "id": 91,
                    "name": "Task type",
                    "objectTypeNameM": "task.type_id",
                    "required": True,
                },
                {"id": 100, "name": "Title", "objectTypeNameM": "task.task_title"},
                {"id": 101, "name": "Description", "objectTypeNameM": "task.content"},
            ],
        }
    ],
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


def test_the_action_reaches_sentrys_rule_registry() -> None:
    """The action is in the live registry, put there by `JagaAppConfig.ready()`; this asserts the
    result rather than the route, so it fails loudly if that hook ever stops firing."""
    by_id = {cls.id: cls for _, cls in rules}

    assert ACTION_ID in by_id, "the rule never reached the registry"
    assert by_id[ACTION_ID] is JagaCreateTicketAction
    assert rules.get(ACTION_ID) is JagaCreateTicketAction


def test_the_action_is_registered_exactly_once() -> None:
    """`RuleRegistry.add` appends to a list, so a double registration would show the action twice
    in the alert-rule action picker."""
    assert [cls for _, cls in rules if cls is JagaCreateTicketAction] == [JagaCreateTicketAction]


class JagaTicketRuleTest(RuleTestCase, BaseAPITestCase):
    rule_cls = JagaCreateTicketAction

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
        self.login_as(user=self.user)

    def test_action_presents_itself_as_a_ticket_action(self) -> None:
        rule = self.get_rule(data={"integration": self.integration.id})

        assert rule.ticket_type == "a Jaga task"
        assert rule.prompt == "Create a Jaga task"
        # `render_label` is the line the rule list shows; it must name the integration.
        assert "Jaga" in rule.render_label()

    def test_footer_points_back_at_the_rule_that_filed_the_task(self) -> None:
        rule = self.get_rule(data={"integration": self.integration.id})
        footer = rule.generate_footer("/organizations/x/alerts/rules/y/1/")

        assert "Sentry alert rule" in footer
        assert "/organizations/x/alerts/rules/y/1/" in footer

    @responses.activate
    def test_firing_the_rule_creates_a_jaga_task_and_links_it_to_the_group(self) -> None:
        """End to end: Sentry fires the action and a Jaga task appears, linked to the group. The
        rule is saved WITHOUT a title — Sentry fills it from the event at fire time — so a form
        that named the title anything but `title` would file a nameless task."""
        responses.add(responses.POST, f"{API}/v1/auth/login", json=AUTH_OK, status=200)
        responses.add(responses.GET, f"{API}/v1/project/1/taskType/10", json=ATTRS_RESPONSE)
        responses.add(
            responses.POST, f"{API}/v1/task/createByTaskType/1/10", json=CREATED_TASK, status=200
        )

        # Load-bearing: the rule serializer resolves `id` against the rule registry, so a 200 here
        # proves the action really is registered, not merely importable.
        response = self.client.post(
            reverse(
                "sentry-api-0-project-rules",
                kwargs={
                    "organization_id_or_slug": self.organization.slug,
                    "project_id_or_slug": self.project.slug,
                },
            ),
            format="json",
            data={
                "name": "file a Jaga task",
                "owner": self.user.id,
                "environment": None,
                "actionMatch": "any",
                "frequency": 5,
                "conditions": [],
                "actions": [
                    {
                        "id": ACTION_ID,
                        "integration": self.integration.id,
                        # What the ticket-rule modal saves: the cascade, and no title.
                        "project": "1",
                        "issue_type": "10",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.content

        rule_object = Rule.objects.get(id=response.data["id"])
        event = self.get_group_event()

        action = rule_object.data.get("actions", ())[0]
        action_inst = self.get_rule(data=action, rule=rule_object)
        results = list(action_inst.after(event=event))
        assert len(results) == 1
        results[0].callback(event, futures=[RuleFuture(rule=rule_object, kwargs=results[0].kwargs)])

        external_issue = ExternalIssue.objects.get(
            integration_id=self.integration.id, key="PLT-500"
        )
        assert GroupLink.objects.filter(
            group_id=event.group.id, linked_id=external_issue.id
        ).exists()

        create_call = next(c for c in responses.calls if "createByTaskType" in c.request.url)
        cells = {
            item["fieldId"]: item["value"]
            for item in json.loads(create_call.request.body)["attributes"]
        }

        # The title Jaga got is the event's own; the description carries the Sentry link and the
        # footer naming the rule.
        assert cells[100] == event.group.title
        assert "Sentry issue:" in cells[101]
        assert "Sentry alert rule" in cells[101]
        # Jaga rejects a create whose `attributes` omit the space and the type.
        assert cells[90] == 1
        assert cells[91] == 10
