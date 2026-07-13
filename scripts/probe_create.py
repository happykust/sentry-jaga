"""Find out the minimal attribute payload Jaga accepts when creating a task.

The task type declares `task.project_id` and `task.type_id` as *required* attributes, yet
both are already given in the URL of `POST /v1/task/createByTaskType/{projectId}/{taskTypeId}`.
Whether Jaga still expects them inside the `attributes` list is not answerable from the
OpenAPI spec — only a real create answers it, and the answer decides how the plugin builds
its form.

WARNING: this WRITES. It creates one real task in the space you name, then offers to close it.

Usage:
    export JAGA_URL=https://jaga.rt.ru
    export JAGA_EMAIL='...'
    export JAGA_SPACE_ID=<space id from probe_jaga.py>
    export JAGA_TYPE_ID=<task type id from probe_jaga.py>
    uv run python scripts/probe_create.py     # password is asked for interactively
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from typing import Any

from sentry_jaga.client.api import JagaClient
from sentry_jaga.client.exceptions import JagaApiError, JagaError
from sentry_jaga.fields import DESCRIPTION_OBJECT_TYPE, TITLE_OBJECT_TYPE

TITLE = "[sentry-jaga probe] please ignore"
BODY = "Created by scripts/probe_create.py to verify the required-attribute rules."


def attempt(
    client: JagaClient,
    space_id: int,
    type_id: int,
    attributes: list[dict[str, Any]],
    label: str,
) -> int | None:
    """Try one create. Returns the new task id, or None if Jaga refused."""
    print(f"\n== Attempt: {label} ==")
    print(f"   attributes sent: {[a['fieldId'] for a in attributes]}")
    try:
        task = client.create_task(space_id, type_id, attributes)
    except JagaApiError as exc:
        print(f"   REFUSED — HTTP {exc.status_code}")
        print(f"   {json.dumps(exc.body, ensure_ascii=False)[:400]}")
        return None
    print(f"   ACCEPTED — created {task.code} (id={task.id})")
    return task.id


def main() -> int:
    url = os.environ.get("JAGA_URL")
    email = os.environ.get("JAGA_EMAIL")
    if not (url and email):
        print("Set JAGA_URL and JAGA_EMAIL first.", file=sys.stderr)
        return 2

    password = os.environ.get("JAGA_PASSWORD") or getpass.getpass("Jaga password: ")
    client = JagaClient(instance_url=url, email=email, password=password)

    spaces = {s.id: s for s in client.get_projects()}

    # No space chosen yet: list what this account can write to, and stop. Nothing is created.
    space_id = os.environ.get("JAGA_SPACE_ID")
    if not space_id:
        print("\nSpaces visible to this account — pick a TEST one, not a production space:\n")
        for s in spaces.values():
            print(f"   JAGA_SPACE_ID={s.id:<6} {s.title}  ({s.code})")
        print("\nRe-run with JAGA_SPACE_ID set to see that space's task types.")
        return 0

    space = int(space_id)
    target = spaces.get(space)
    if target is None:
        print(
            f"Space id={space} is not among the spaces this account can see: {sorted(spaces)}",
            file=sys.stderr,
        )
        return 1

    types = {t.id: t for t in client.get_task_types(space)}

    # Space chosen, but no task type yet: list the types of that space, and stop.
    type_id = os.environ.get("JAGA_TYPE_ID")
    if not type_id:
        print(f"\nTask types in space '{target.title}' ({target.code}):\n")
        for t in types.values():
            print(f"   JAGA_TYPE_ID={t.id:<6} {t.name}")
        print("\nRe-run with JAGA_TYPE_ID set to run the create probe.")
        return 0

    ttype = int(type_id)
    target_type = types.get(ttype)
    if target_type is None:
        print(
            f"Task type id={ttype} does not exist in space '{target.title}'. "
            f"Available: {sorted(types)}",
            file=sys.stderr,
        )
        return 1

    print("\n" + "!" * 70)
    print("THIS WILL CREATE A REAL TASK. Check the target before continuing:")
    print(f"    space:     {target.title}  ({target.code})   id={target.id}")
    print(f"    task type: {target_type.name}   id={target_type.id}")
    print(f"    title:     {TITLE}")
    print("!" * 70)
    if input("Create it there? [y/N] ").strip().lower() != "y":
        print("Aborted, nothing was written.")
        return 0

    attrs = client.get_task_type_attributes(space, ttype)
    by_mnemonic = {a.object_type_name_m: a for a in attrs}

    title_attr = by_mnemonic.get(TITLE_OBJECT_TYPE)
    body_attr = by_mnemonic.get(DESCRIPTION_OBJECT_TYPE)
    if title_attr is None:
        print(f"Task type has no {TITLE_OBJECT_TYPE} attribute; cannot probe.", file=sys.stderr)
        return 1

    print("Required attributes declared by this task type:")
    for a in attrs:
        if a.required:
            print(f"   {a.object_type_name_m:25} {a.name}  (fieldId={a.id})")

    def cell(attr_id: int, value: Any, *, reference: bool = False) -> dict[str, Any]:
        return {"fieldId": attr_id, "value": value, "referenceValue": reference, "addInfo": {}}

    # Attempt 1: only what the plugin can meaningfully fill — title and description.
    minimal = [cell(title_attr.id, TITLE)]
    if body_attr is not None:
        minimal.append(cell(body_attr.id, BODY))

    task_id = attempt(client, space, ttype, minimal, "title + description only")

    # Attempt 2: if refused, add the space/type attributes that duplicate the URL path.
    if task_id is None:
        extra = list(minimal)
        for mnemonic, value in (("task.project_id", space), ("task.type_id", ttype)):
            attr = by_mnemonic.get(mnemonic)
            if attr is not None:
                extra.append(cell(attr.id, value, reference=True))
        task_id = attempt(client, space, ttype, extra, "+ task.project_id and task.type_id")

    print("\n== Verdict ==")
    if task_id is None:
        print("  Jaga refused both payloads. Read the errors above: they name what is missing.")
        return 1
    print("  A task can be created with the attributes shown in the accepted attempt.")
    print(f"  Open it and check the title/description landed correctly: {url}/task/...")

    if input("\nClose the probe task now? [y/N] ").strip().lower() == "y":
        try:
            client._authed("DELETE", f"/v1/task/{task_id}")
        except JagaError as exc:
            print(f"  Could not close it: {exc}. Close it by hand.")
        else:
            print("  Closed.")
    else:
        print("  Left open — remember to close it by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
