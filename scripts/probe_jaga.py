"""Probe a live Jaga instance to validate the assumptions the plugin is built on.

This script needs no Sentry at all: the client core is deliberately independent of it.

Usage:
    export JAGA_URL=https://jaga.example.com
    export JAGA_EMAIL=service-account@example.com
    uv run python scripts/probe_jaga.py     # the password is asked for interactively

The password is read with getpass rather than from the environment on purpose: a shell
mangles secrets containing $, !, ` or \\, which looks exactly like a wrong password.

It answers three questions the test suite cannot:
  1. Do the service-account credentials work, and does the token flow behave?
  2. What are the REAL `objectTypeNameM` mnemonics of a task type's attributes?
     The plugin assumes `task.title` and `task.content_data`. If they differ,
     the title/description of a Sentry issue will not be pre-filled into the form.
  3. What does a task actually look like, so the "open in Jaga" URL can be confirmed?

Nothing is written to Jaga: the probe only reads.
"""

from __future__ import annotations

import getpass
import os
import sys

import requests

from sentry_jaga.client.api import JagaClient
from sentry_jaga.client.exceptions import JagaError
from sentry_jaga.fields import DESCRIPTION_OBJECT_TYPE, TITLE_OBJECT_TYPE


def main() -> int:
    url = os.environ.get("JAGA_URL")
    email = os.environ.get("JAGA_EMAIL")
    if not (url and email):
        print("Set JAGA_URL and JAGA_EMAIL first.", file=sys.stderr)
        return 2

    password = os.environ.get("JAGA_PASSWORD") or getpass.getpass("Jaga password: ")

    client = JagaClient(instance_url=url, email=email, password=password)

    print("== 1. Authentication ==")
    print(f"  POST {url.rstrip('/')}/external-api/v1/auth/login")
    print(f"  email:    {email!r}")
    risky = sorted({c for c in password if c in "$!`\\\"'"})
    print(
        f"  password: {len(password)} chars"
        + (f", contains shell-sensitive chars: {' '.join(risky)}" if risky else "")
    )
    print("  (compare these with exactly what you typed into Swagger)")
    try:
        token = client.login()
    except JagaError as exc:
        print(f"  FAILED: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"  FAILED: cannot reach Jaga: {exc}", file=sys.stderr)
        return 1
    print(f"  ok, token expires at {token.expires_at.isoformat()}")

    print("\n== 2. Spaces visible to the service account ==")
    spaces = client.get_projects()
    if not spaces:
        print("  none — the account has access to no space; the create form would be empty.")
        return 1
    for space in spaces[:10]:
        print(f"  [{space.id}] {space.title} ({space.code})")
    print(f"  total: {len(spaces)}")

    space = spaces[0]
    print(f"\n== 3. Task types of space '{space.title}' ==")
    task_types = client.get_task_types(space.id)
    for task_type in task_types:
        print(f"  [{task_type.id}] {task_type.name}")
    if not task_types:
        print("  none")
        return 1

    task_type = task_types[0]
    print(f"\n== 4. THE KEY CHECK: attributes of task type '{task_type.name}' ==")
    print("   (the plugin identifies the title/description fields by objectTypeNameM)")
    attributes = client.get_task_type_attributes(space.id, task_type.id)
    for attr in attributes:
        flags = []
        if attr.required:
            flags.append("required")
        if attr.multiple:
            flags.append("multiple")
        if not attr.visible:
            flags.append("hidden")
        if attr.dictionary_id is not None:
            flags.append(f"dict={attr.dictionary_id}")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {attr.object_type_name_m or '(empty)':35} {attr.name}{suffix}")

    mnemonics = {a.object_type_name_m for a in attributes}
    print("\n== 5. Verdict on the plugin's assumptions ==")
    for label, expected in (("title", TITLE_OBJECT_TYPE), ("description", DESCRIPTION_OBJECT_TYPE)):
        if expected in mnemonics:
            print(f"  OK       {label}: '{expected}' exists — pre-fill will work")
        else:
            print(f"  MISMATCH {label}: '{expected}' NOT found in this task type.")
            print(
                f"           Pick the right one from the list above and change "
                f"{'TITLE_OBJECT_TYPE' if label == 'title' else 'DESCRIPTION_OBJECT_TYPE'} "
                f"in src/sentry_jaga/fields.py"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
