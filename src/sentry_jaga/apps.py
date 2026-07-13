"""Django app for the package. Registered through the `sentry.apps` entry point.

It does two jobs: it lets Django find the installation-form template
(`sentry_jaga/templates/sentry_jaga/config.html`), and — in `ready()` — it puts the
alert-rule action into Sentry's rule registry. See `_register_rules` for why that has to
happen here and nowhere else.
"""

from __future__ import annotations

from django.apps import AppConfig


def _register_rules() -> None:
    """Add the Jaga ticket action to Sentry's rule registry.

    THE OBVIOUS ROUTE DOES NOT WORK. Sentry builds the registry in `sentry.rules.init_registry`
    from a hardcoded list plus every legacy v2 plugin's `get_rules()`:

        for rule in _SENTRY_RULES:              # sentry.constants, not extensible
            registry.add(import_string(rule))
        for plugin in plugins.all(version=2):   # the documented door for outside packages
            for cls in safe_execute(plugin.get_rules) or ():
                registry.add(cls)

    That second loop can never see an out-of-tree plugin on Sentry 26.3.1. `init_registry()`
    runs at *import* of `sentry.rules`, and `sentry.rules` is imported during `django.setup()`
    — `sentry.plugins.sentry_interface_types` (a default INSTALLED_APP) reaches it through
    `sentry.plugins.bases.issue2` -> `sentry.issues.endpoints` ->
    `api.serializers.rest_framework.rule`.
    `django.setup()` is line 342 of `sentry/runner/initializer.py`; `register_plugins()`, which
    is what registers plugins from the `sentry.plugins` entry point, is line 356. The registry
    is therefore always built from an empty plugin manager, and it is a module-level singleton
    that is never rebuilt.

    `AppConfig.ready()` is the hook that does land in time. Django's `populate()` imports every
    app's models (phase 2 — this is what drags `sentry.rules` in and builds the registry) and
    only then calls `ready()` on each app (phase 3). By the time we get here the registry object
    exists and is live, so adding to it sticks — no plugin, and no dependence on the order in
    which `initialize_app` happens to do things.
    """
    from sentry.rules import rules

    from sentry_jaga.notify_action import JagaCreateTicketAction

    # `RuleRegistry.add` appends to a list, so a second call would list the action twice.
    # Django calls `ready()` once per process, but say what we mean.
    if JagaCreateTicketAction.id not in rules:
        rules.add(JagaCreateTicketAction)


class JagaAppConfig(AppConfig):
    name = "sentry_jaga"
    verbose_name = "Sentry Jaga Integration"

    def ready(self) -> None:
        _register_rules()
