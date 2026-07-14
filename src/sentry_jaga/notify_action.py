"""The alert-rule action: "Create a Jaga task ...".

`TicketEventAction` supplies everything but the naming. Its `after()` yields a future onto
`create_ticket.utils.create_issue`, which calls our own `installation.create_issue(data)` and then
records the `ExternalIssue` and the `GroupLink` — so a rule-filed task is linked exactly like a
hand-filed one, and the status sync (`sync.py`) picks it up from there.

That utility overwrites `data["title"]` and `data["description"]` with the title and body of the
event that fired the rule, which is why the Jaga title and content attributes must be named `title`
and `description` in the create form — see `CANONICAL_FIELD_NAMES` in `fields.py`.

The action is registered from `JagaAppConfig.ready()`, not through the `Plugin2.get_rules()` door
Sentry documents for it — see `apps.py::_register_rules`.
"""

from __future__ import annotations

from sentry.rules.actions import TicketEventAction
from sentry.rules.actions.integrations.create_ticket.form import IntegrationNotifyServiceForm
from sentry.utils.http import absolute_uri


class JagaCreateTicketAction(TicketEventAction):
    # The id is the string a saved rule stores, so it is frozen: renaming the class or moving the
    # module must not orphan the rules already in the database.
    id = "sentry_jaga.notify_action.JagaCreateTicketAction"
    label = "Create a Jaga task in {integration} with these "
    ticket_type = "a Jaga task"
    link = "https://github.com/happykust/sentry-jaga#alert-rules"
    provider = "jaga"

    def generate_footer(self, rule_url: str) -> str:
        return (
            "\n\nThis task was created automatically by a Sentry alert rule: "
            f"{absolute_uri(rule_url)}"
        )

    def get_form_instance(self) -> IntegrationNotifyServiceForm:
        return IntegrationNotifyServiceForm(self.data, integrations=self.get_integrations())
