"""Django app for the package, registered through the `sentry.apps` entry point.

It lets Django find the installation-form template, and in `ready()` puts the alert-rule action
into Sentry's rule registry — see `_register_rules` for why that has to happen there.
"""

from __future__ import annotations

from django.apps import AppConfig


def _register_rules() -> None:
    """Add the Jaga ticket action to Sentry's rule registry.

    The documented door — a v2 plugin's `get_rules()` — is nailed shut on Sentry 26.3.1:
    `init_registry()` runs at *import* of `sentry.rules`, which happens during `django.setup()`,
    while `register_plugins()` runs only afterwards. The registry is therefore always built from an
    empty plugin manager, and it is a module-level singleton that is never rebuilt.

    `AppConfig.ready()` does land in time: Django imports every app's models first (which is what
    drags `sentry.rules` in and builds the registry) and calls `ready()` after, so the registry
    exists and is live by then and adding to it sticks.
    """
    from sentry.rules import rules

    from sentry_jaga.notify_action import JagaCreateTicketAction

    # `RuleRegistry.add` appends to a list, so a second call would list the action twice.
    if JagaCreateTicketAction.id not in rules:
        rules.add(JagaCreateTicketAction)


class JagaAppConfig(AppConfig):
    name = "sentry_jaga"
    verbose_name = "Sentry Jaga Integration"

    def ready(self) -> None:
        _register_rules()
