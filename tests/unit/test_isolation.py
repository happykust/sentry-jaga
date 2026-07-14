"""The core (client/, fields, descriptions, issue_config) must not import sentry."""

import subprocess
import sys

CORE_MODULES = [
    "sentry_jaga.client",
    "sentry_jaga.client.api",
    "sentry_jaga.client.auth",
    "sentry_jaga.client.exceptions",
    "sentry_jaga.client.models",
    "sentry_jaga.descriptions",
    "sentry_jaga.fields",
    "sentry_jaga.issue_config",
]


def test_core_modules_do_not_import_sentry() -> None:
    """The whole core imported in a clean process must not pull sentry in."""
    code = (
        "import sys;"
        + "".join(f"import {m};" for m in CORE_MODULES)
        + "leaked = [m for m in sys.modules if m == 'sentry' or m.startswith('sentry.')];"
        + "print(','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    leaked = [m for m in result.stdout.strip().split(",") if m]
    assert leaked == [], f"The core pulled in sentry: {leaked}"
