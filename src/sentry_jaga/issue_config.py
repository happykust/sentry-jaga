"""Issue-layer logic: building Sentry forms and operating on Jaga tasks.

This module is framework-agnostic — it does NOT import `sentry`. The integration-layer
classes (`issues.py`, `sync.py`) are thin delegates on top of these functions. That way
all the real logic is covered by unit tests without Sentry's test stack
(Postgres/Kafka/Snuba).

The field-dict format is the one Sentry's frontend understands (`ExternalIssueForm`):
name, label, type (string|textarea|select), default, choices, required,
multiple, updatesForm, help.
"""

from __future__ import annotations

import logging
from typing import Any

from sentry_jaga.client.api import JagaClient
from sentry_jaga.client.exceptions import JagaError
from sentry_jaga.client.models import Attribute, Project, Status
from sentry_jaga.descriptions import build_link_comment
from sentry_jaga.fields import (
    ASSIGNEE_OBJECT_TYPE,
    DESCRIPTION_OBJECT_TYPE,
    LABEL_OBJECT_TYPE,
    SPACE_OBJECT_TYPE,
    TITLE_OBJECT_TYPE,
    build_attribute_fields,
    extract_title,
    field_name,
    find_attribute,
    form_data_to_attributes,
    injected_attributes,
    merge_auto_label,
)

logger = logging.getLogger("sentry_jaga.issue_config")

SEARCH_LIMIT = 20
# `updatesForm` makes Sentry's frontend re-fetch the config on every keystroke. There is
# nowhere to hook up a debounce of our own, so we damp the noise with a minimum query
# length instead.
MIN_QUERY_LENGTH = 3
RESOLVED_COMMENT = "The Sentry issue has been resolved. This task can be completed."
UNRESOLVED_COMMENT = "The Sentry issue has been reopened: the error happened again."

# The status *categories* Jaga groups every status under. The sync maps a Sentry resolve onto
# one of these — not onto a status id — because a status id is meaningless outside its own
# workflow: a live instance carries ~90k statuses across ~15k workflows, and they are all
# variations on these three categories. Mapping by category means one setting for the whole
# organization; mapping by id would mean a per-space mapping table (what Jira Server does) and
# an unusable dropdown of 90k entries.
CATEGORY_TODO = "status.category.todo"
CATEGORY_IN_PROGRESS = "status.category.inprogress"
CATEGORY_DONE = "status.category.done"

# The create-form fields whose last used value is remembered per Sentry project. Sentry stores
# them itself (`IssueBasicIntegration.store_issue_last_defaults`), keyed by the names below —
# which is why they must stay in step with the field names `build_create_config` emits.
PERSISTED_FIELDS = ("project", "issue_type")

# The label every task created from Sentry is tagged with, unless the organization says
# otherwise. An empty setting means no label at all. The default lives here, in the core,
# because two places must agree on it: the org-config field that renders it (`sync.py`) and the
# fallback the create falls back to before that config was ever saved (`issues.py`).
DEFAULT_AUTO_LABEL = "sentry"

# The hidden field that carries the Sentry issue through the create form and back to
# `create_issue`, which is otherwise handed the form data and NOTHING else — no group, no event
# (there is no "after create, with the event" hook anywhere in Sentry's issue contract).
#
# It is emitted only when there IS a group (see `build_create_config`), which is not a detail:
# the alert-rule ticket modal renders this very form with `group=None`
# (`IntegrationSerializer`: 'Query param "action" only attached in TicketRuleForm modal'), and
# whatever the form returns is SAVED INTO THE RULE. A group id baked in there would be frozen
# forever, and every task the rule ever filed would get the event of one long-dead issue
# attached. No field, no attachment — the honest outcome.
GROUP_ID_FIELD = "sentry_group_id"

EVENT_ATTACHMENT_CONTENT_TYPE = "application/json"


class NoProjectsError(JagaError):
    """The service account has no spaces available in Jaga."""


def _project_choices(projects: list[Project]) -> list[tuple[str, str]]:
    return [(str(p.id), f"{p.title} ({p.code})") for p in projects]


def _selected_id(params: dict[str, Any], key: str, available: list[int]) -> int:
    """The id selected in `params` — but only if it is among the available ones.

    With `updatesForm`, Sentry's frontend resends EVERY form field, not just the one that
    changed. After the space is switched, `params` still carries the `issue_type` of the
    previous one: taking it at face value means a 404 from Jaga or, worse, silently
    creating a task whose type belongs to another space. So the value is validated against
    the current list, and on a miss we fall back to the first available one.

    `available` is never empty: the caller guarantees that.
    """
    raw: Any = params.get(key)
    try:
        candidate = int(raw)
    except (TypeError, ValueError):  # key absent (None), empty, or not a number
        return available[0]
    return candidate if candidate in available else available[0]


def _with_defaults(params: dict[str, Any], defaults: dict[str, Any] | None) -> dict[str, Any]:
    """The form selection to render: what the user is doing now, over what they did last time.

    `defaults` are the values Sentry remembered from this project's last create (see
    `PERSISTED_FIELDS`); `params` is the live state of the form. `params` must win — with
    `updatesForm` the frontend resends every field on every keystroke, so the moment the user
    touches the space select, `params` carries their choice and the remembered one is history.

    Empty values in `params` are dropped rather than allowed to win. On the first render the
    frontend sends no space at all, but a blank is also what an unset select serialises to, and
    a blank taken at face value would silently throw the remembered choice away — reinstating
    the very bug this is here to fix.
    """
    merged = dict(defaults or {})
    merged.update({key: value for key, value in params.items() if value not in (None, "")})
    return merged


def _require_projects(client: JagaClient) -> list[Project]:
    projects = client.get_projects()
    if not projects:
        raise NoProjectsError("This service account has no spaces available in Jaga.")
    return projects


def _has_visible(attributes: list[Attribute], object_type: str) -> bool:
    """Is this attribute both present on the task type and shown in the form?

    Gates the extra fetches for assignees and labels: a task type that has no such attribute
    (or hides it) must not cost an HTTP request per form render.
    """
    attr = find_attribute(attributes, object_type)
    return attr is not None and attr.visible


def _warn_if_no_system_attributes(
    attributes: list[Attribute], project_id: int, type_id: int
) -> None:
    """Warn if not a single system attribute was recognised on the task type.

    The mnemonics `task.task_title` / `task.content` are confirmed against a live instance,
    but they are what a Jaga deployment *happened* to expose, not a documented contract. If a
    deployment renames them, the form quietly comes out without a title and a description —
    and the Sentry context never reaches the task. Let the miss be visible in the logs.
    """
    if find_attribute(attributes, TITLE_OBJECT_TYPE) or find_attribute(
        attributes, DESCRIPTION_OBJECT_TYPE
    ):
        return
    logger.warning(
        "jaga.issue_config.system_attributes_not_found",
        extra={
            "project_id": project_id,
            "task_type_id": type_id,
            "expected": [TITLE_OBJECT_TYPE, DESCRIPTION_OBJECT_TYPE],
            "seen": [attr.object_type_name_m for attr in attributes],
        },
    )


def _project_field(projects: list[Project], project_id: int) -> dict[str, Any]:
    return {
        "name": "project",
        "label": "Space",
        "type": "select",
        "choices": _project_choices(projects),
        "default": str(project_id),
        "required": True,
        "updatesForm": True,
    }


def _group_id_field(group_id: str | None) -> list[dict[str, Any]]:
    """The Sentry issue, carried through the form as a hidden field — when there is one.

    `hidden` is a field type Sentry's frontend knows (`static/app/components/forms/types.tsx`);
    it renders as a `display: none` input whose default is submitted along with the rest of the
    form. That is the whole trick: `create_issue` gets the form data and nothing else, so this is
    the only way the issue can reach it.

    None means no field at all — see `GROUP_ID_FIELD` for why that matters for alert rules.
    """
    if group_id is None:
        return []
    return [{"name": GROUP_ID_FIELD, "type": "hidden", "default": group_id}]


def build_create_config(
    client: JagaClient,
    params: dict[str, Any],
    title: str,
    description: str,
    defaults: dict[str, Any] | None = None,
    group_id: str | None = None,
) -> list[dict[str, Any]]:
    """The create-form cascade: space -> task type -> dynamic attributes.

    `defaults` carries the space and the task type this Sentry project last created a task with,
    so that a team that always files into one space does not have to re-pick it every time. They
    are only a starting point: `_selected_id` still validates them against what Jaga offers
    *now*, so a remembered space the service account has since lost access to falls back to the
    first available one instead of rendering a form that cannot be submitted.

    `group_id` is the Sentry issue the form was opened from, if any — see `_group_id_field`.
    """
    selection = _with_defaults(params, defaults)
    projects = _require_projects(client)
    project_id = _selected_id(selection, "project", [p.id for p in projects])
    fields: list[dict[str, Any]] = [
        _project_field(projects, project_id),
        *_group_id_field(group_id),
    ]

    task_types = client.get_task_types(project_id)
    if not task_types:
        return fields

    # Types are validated against the list of the CURRENT space: see `_selected_id`.
    type_id = _selected_id(selection, "issue_type", [t.id for t in task_types])
    fields.append(
        {
            "name": "issue_type",
            "label": "Task type",
            "type": "select",
            "choices": [(str(t.id), t.name) for t in task_types],
            "default": str(type_id),
            "required": True,
            "updatesForm": True,
        }
    )

    attributes = client.get_task_type_attributes(project_id, type_id)
    _warn_if_no_system_attributes(attributes, project_id, type_id)
    choices_by_dictionary = {
        attr.dictionary_id: client.get_dictionary_values(attr.dictionary_id)
        for attr in attributes
        if attr.dictionary_id is not None and attr.visible
    }
    # Assignees and labels carry no `dictionaryId`: their values come from endpoints of their
    # own. Only pay for them when the task type actually shows the attribute.
    user_choices = (
        client.get_space_users(project_id)
        if _has_visible(attributes, ASSIGNEE_OBJECT_TYPE)
        else None
    )
    label_choices = client.get_labels() if _has_visible(attributes, LABEL_OBJECT_TYPE) else None

    fields.extend(
        build_attribute_fields(
            attributes,
            choices_by_dictionary,
            title,
            description,
            user_choices=user_choices,
            label_choices=label_choices,
        )
    )
    return fields


def create_task_from_form(
    client: JagaClient, form_data: dict[str, Any], *, auto_label: str = ""
) -> dict[str, Any]:
    """Create a Jaga task from the data submitted in a Sentry form.

    `auto_label` is the name of the label every task filed from Sentry carries, so that all of
    them can be found in Jaga with one filter (`DEFAULT_AUTO_LABEL`; an empty string turns it
    off). It is resolved to an id here rather than in the form, because the label may not exist
    in Jaga yet — `get_or_create_label` makes it on first use.

    The label is NOT looked up when the task type has no labels attribute: not every type does,
    and a form render must not cost an HTTP call that can only end in a cell Jaga would reject.
    """
    project_id = int(form_data["project"])
    type_id = int(form_data["issue_type"])

    attributes = client.get_task_type_attributes(project_id, type_id)
    payload = form_data_to_attributes(form_data, attributes)
    if not payload:
        raise JagaError("Not a single task attribute was filled in.")

    # Deliberately not gated on the attribute being `visible`: a type that hides its labels from
    # the form still has them, and the point of the auto-label is that EVERY task from Sentry
    # carries it. The user cannot have chosen anything there, so there is nothing to merge with.
    label = auto_label.strip()
    if label and find_attribute(attributes, LABEL_OBJECT_TYPE) is not None:
        merge_auto_label(payload, attributes, client.get_or_create_label(label))

    # The space and the task type have no form field of their own, so nothing above produces
    # them — and Jaga answers 500 to a create that leaves them out of `attributes`, even
    # though both ids are right there in the URL. See `injected_attributes`.
    payload.extend(injected_attributes(attributes, project_id, type_id))

    task = client.create_task(project_id, type_id, payload)

    title_attr = find_attribute(attributes, TITLE_OBJECT_TYPE)
    title = str(form_data.get(field_name(title_attr), "")) if title_attr else task.code
    # `metadata` travels into `ExternalIssue`: `task_id` is Jaga's own id for the task, which
    # nothing but Jaga can reconstruct from the code. It is NOT a shortcut for the status sync —
    # that one has to fetch the task anyway, for its `statusTransitions` and its space (see
    # `apply_status_sync`) — it is the stable handle on the task, kept for the logs and for a
    # future inbound sync. Cf. `get_task_summary`, which records the same thing on a link.
    return {
        "key": task.code,
        "title": title,
        "description": "",
        "metadata": {"task_id": task.id},
    }


def _event_attachment_name(event_id: str) -> str:
    """The file name the event is attached under.

    The event id is in the name on purpose: a task can be filed from an issue more than once,
    and two attachments called `sentry-event.json` on the same task tell nobody which event is
    which. An event with no id is not a thing Sentry produces, but the fallback costs a line.
    """
    return f"sentry-event-{event_id}.json" if event_id else "sentry-event.json"


def attach_event_json(
    client: JagaClient, *, space_id: int, task_id: int, event_id: str, content: bytes
) -> dict[str, Any]:
    """Attach the JSON of a Sentry event to a task that has just been created.

    The serialization is the Sentry layer's job (only it can see an event); what the core owns is
    the name, the content type and the call. Jaga wants the space as well as the task: attachments
    are filed under a space (see `JagaClient.attach_file`).
    """
    return client.attach_file(
        space_id,
        task_id,
        _event_attachment_name(event_id),
        content,
        EVENT_ATTACHMENT_CONTENT_TYPE,
    )


def _comment_field(sentry_url: str | None) -> list[dict[str, Any]]:
    """The comment posted on the task when it is linked — as an editable, clearable field.

    Every issue integration upstream (Jira Server, GitHub, GitLab, Bitbucket) does exactly this,
    and the shape is not an accident: `after_link_issue` is handed the form data and nothing
    else — not the group, not the URL — so the only way the Sentry link can reach it is baked
    into this field's default at render time, when the group *is* in hand.

    That the field is editable is the feature, not a compromise: it doubles as the per-link
    opt-out. Someone linking a task in a space where a Sentry URL means nothing to anybody just
    clears the box, and no comment is posted — which is why this needs no organization-wide
    toggle of its own.
    """
    if sentry_url is None:
        return []
    return [
        {
            "name": "comment",
            "label": "Comment",
            "type": "textarea",
            "default": build_link_comment(sentry_url),
            "required": False,
            "autosize": True,
            "maxRows": 10,
            "help": "Posted on the Jaga task when it is linked. Clear it to post nothing.",
        }
    ]


def build_link_config(
    client: JagaClient,
    params: dict[str, Any],
    search_url: str | None = None,
    sentry_url: str | None = None,
) -> list[dict[str, Any]]:
    """Fields of the link form: a way to find a task — anywhere in Jaga — and a comment.

    There is deliberately no space select here. Linking used to open with one, because Jaga's
    per-space search (`/v1/task/searchByTitleCode`) demands a `projectId`: you had to remember
    which space the task lived in before you could look for it. The global search does not, so
    the space is gone from the form — you type, and the task is found wherever it is.

    The cost of that is honest and small: the global search returns a task's space as `null`
    (see `JagaClient.search_tasks_globally`), so a suggestion can only read `code — title`.
    Fetching each hit's space one by one, on every keystroke, to decorate a dropdown is not a
    trade worth making.

    Two ways to search, and which one we get depends on something outside this package: whether
    the admin pointed `ROOT_URLCONF` at `sentry_jaga.urlconf` (see `urlconf.py`).

    * `search_url` given — the real thing. A `url` on a `select` field is what makes Sentry's
      frontend treat it as an async select: it calls the endpoint as the user types, with its
      own debounce, and renders the results. No `query` field is needed, and no search — indeed
      no call to Jaga at all — runs while the form is merely being rendered.

    * `search_url` is None — the fallback, and the only thing that worked before the endpoint
      existed. The query is a plain text field with `updatesForm`, so Sentry re-fetches the
      WHOLE config on every keystroke (there is no debounce on that path, and an external
      package cannot ship JS to add one). We soften it on the server: nothing is searched below
      `MIN_QUERY_LENGTH`.

    The core must not know what a URL route is, let alone import `sentry`, so the caller
    (`issues.py`) resolves it and passes it down.
    """
    if search_url is not None:
        return [
            {
                "name": "externalIssue",
                "label": "Task",
                "type": "select",
                # `url` IS the feature: `getFieldProps` in Sentry's frontend switches the
                # select to async the moment it sees one. Without it the key is inert.
                "url": search_url,
                "required": True,
                "help": (
                    f"Start typing a task code or part of a title — every space is searched, "
                    f"from {MIN_QUERY_LENGTH} characters."
                ),
            },
            *_comment_field(sentry_url),
        ]

    query = str(params.get("query") or "").strip()
    choices: list[tuple[str, str]] = []
    if len(query) >= MIN_QUERY_LENGTH:
        choices = [
            (task.code, f"{task.code} — {task.title}")
            for task in client.search_tasks_globally(query, size=SEARCH_LIMIT)
        ]

    return [
        {
            "name": "query",
            "label": "Task search",
            "type": "string",
            "default": query,
            "required": False,
            "updatesForm": True,
            "help": (
                f"Enter a task code or part of a task title — every space is searched, "
                f"from {MIN_QUERY_LENGTH} characters."
            ),
        },
        {
            "name": "externalIssue",
            "label": "Task",
            "type": "select",
            "choices": choices,
            "required": True,
            "help": "If the list is empty, refine the search query above.",
        },
        *_comment_field(sentry_url),
    ]


def get_task_summary(client: JagaClient, code: str) -> dict[str, Any]:
    """Summary of a Jaga task for Sentry's `ExternalIssue`."""
    raw = client.get_task_by_code(code)
    return {
        "key": raw["code"],
        "title": extract_title(raw),
        "description": "",
        "metadata": {"task_id": raw["id"]},
    }


def search_task_summaries(client: JagaClient, query: str | None) -> list[dict[str, Any]]:
    """Tasks matching the query, across every space — what the autocomplete endpoint serves.

    No space to scope by, and none in the answer either: the suggestion is `code — title` and
    nothing else. See `build_link_config`.
    """
    if not query:
        return []
    return [
        {"key": task.code, "title": task.title}
        for task in client.search_tasks_globally(query, size=SEARCH_LIMIT)
    ]


def status_comment(is_resolved: bool) -> str:
    return RESOLVED_COMMENT if is_resolved else UNRESOLVED_COMMENT


def extract_space_id(raw_task: dict[str, Any]) -> int | None:
    """The id of the space a task lives in, read off the task itself.

    A task carries its space as an ordinary EAV attribute, so there is no need to remember it
    at link time — which matters, because a task linked before this feature existed has nothing
    but its code stored in Sentry. Verified against a live instance; the value may arrive
    wrapped in a list, as multi-valued attributes do.
    """
    for raw in raw_task.get("attributes", []):
        if raw.get("objectTypeNameM") != SPACE_OBJECT_TYPE:
            continue
        value = raw.get("value")
        if isinstance(value, list):
            value = value[0] if value else None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def reachable_status_ids(raw_task: dict[str, Any]) -> list[int]:
    """The statuses a task can move to from where it stands, deduplicated, order kept.

    Jaga repeats ids in `statusTransitions` (a live instance returned the same id twice), and
    the order is the one the workflow declares — which is the order we want to prefer targets
    in, so it must survive the deduplication.
    """
    ids: list[int] = []
    for raw in raw_task.get("statusTransitions") or []:
        try:
            status_id = int(raw)
        except (TypeError, ValueError):
            continue
        if status_id not in ids:
            ids.append(status_id)
    return ids


def resolve_target_status(
    client: JagaClient, raw_task: dict[str, Any], space_id: int, category: str
) -> Status | None:
    """The status to move a task into: the first REACHABLE one in the wanted category.

    The iteration is over the task's own transitions, not over the statuses of the space: a
    status in the right category that the workflow cannot reach from where the task stands is
    not a target, it is a 4xx waiting to happen. Returning None is a legitimate outcome (a
    workflow with no "done" step out of the current status), and the caller comments instead.
    """
    reachable = reachable_status_ids(raw_task)
    if not reachable:
        return None
    by_id = {status.id: status for status in client.get_space_statuses(space_id)}
    for status_id in reachable:
        status = by_id.get(status_id)
        if status is not None and status.category == category:
            return status
    return None


def apply_status_sync(
    client: JagaClient,
    task_code: str,
    *,
    is_resolved: bool,
    resolved_category: str,
    unresolved_category: str,
    post_comment: bool,
) -> str:
    """Reflect a Sentry status change on the linked Jaga task. Returns what it did.

    The task is always fetched: the transition needs its `statusTransitions` and its space, and
    neither is stored on the Sentry side. Moving the task is the point of the sync, but it is
    not always possible — the workflow may simply have no step from here into the wanted
    category — so a comment is the floor: it is posted whenever the move did not happen, and
    additionally whenever `post_comment` asks for it.
    """
    raw_task = client.get_task_by_code(task_code)
    task_id = int(raw_task["id"])
    space_id = extract_space_id(raw_task)
    category = resolved_category if is_resolved else unresolved_category

    target: Status | None = None
    if space_id is None:
        # Nothing to resolve the status ids against; a comment is all that is left.
        logger.warning(
            "jaga.sync.space_not_found_on_task",
            extra={"task_code": task_code, "task_id": task_id},
        )
    else:
        target = resolve_target_status(client, raw_task, space_id, category)

    if target is not None:
        client.transition_task(task_id, target.id)
        action = f"moved to {target.name!r} (id={target.id})"
    else:
        if space_id is not None:
            # Not an error — but silence here looks exactly like a broken sync from the
            # outside. Name the task, where it stands, and where it could have gone.
            logger.warning(
                "jaga.sync.no_status_in_category",
                extra={
                    "task_code": task_code,
                    "category": category,
                    "current_status": (raw_task.get("status") or {}).get("name"),
                    "reachable_status_ids": reachable_status_ids(raw_task),
                },
            )
        action = f"not moved: no reachable status in category {category!r}"

    # A failed move must never pass silently to the user either: the comment is the fallback.
    commented = post_comment or target is None
    if commented:
        client.create_comment(task_id, status_comment(is_resolved))

    return f"{task_code}: {action}, {'commented' if commented else 'no comment'}"


def _task_id_for(client: JagaClient, task_code: str, task_id: int | None) -> int:
    """Jaga's numeric id for a task, fetching it only when it is not already known.

    Everything on the Sentry side is keyed by the task *code* (`ExternalIssue.key`) — the
    comment endpoints want the numeric id. The id is recorded in `ExternalIssue.metadata` at
    create and at link time (`create_task_from_form`, `get_task_summary`), so the caller can
    usually hand it over; when it cannot — Sentry's comment tasks are given the key and nothing
    else — the task is fetched for it.
    """
    if task_id is not None:
        return int(task_id)
    return int(client.get_task_by_code(task_code)["id"])


def post_task_comment(
    client: JagaClient, task_code: str, content: str, *, task_id: int | None = None
) -> dict[str, Any]:
    """Post a comment on a task and return it as Jaga created it.

    The return value is not decoration: Sentry reads the new comment's id out of it
    (`IssueBasicIntegration.get_comment_id` -> `comment["id"]`) and stores it on the note as
    `external_id`. That id is the only handle `edit_task_comment` below has — without it, an
    edited Sentry note would post a second comment instead of amending the first.
    """
    return client.create_comment(_task_id_for(client, task_code, task_id), content)


def edit_task_comment(
    client: JagaClient,
    task_code: str,
    comment_id: int,
    content: str,
    *,
    task_id: int | None = None,
) -> dict[str, Any]:
    """Rewrite a comment previously posted by `post_task_comment`."""
    return client.update_comment(int(comment_id), _task_id_for(client, task_code, task_id), content)
