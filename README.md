# sentry-jaga

[![CI](https://github.com/happykust/sentry-jaga/actions/workflows/ci.yml/badge.svg)](https://github.com/happykust/sentry-jaga/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sentry-jaga.svg)](https://pypi.org/project/sentry-jaga/)
[![Python](https://img.shields.io/pypi/pyversions/sentry-jaga.svg)](https://pypi.org/project/sentry-jaga/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Integration between self-hosted Sentry and **Jaga**, the issue tracker by Rostelecom.

The package adds an integration provider to Sentry: from an issue you can open a task in Jaga or
link an existing one, and resolving the issue moves the linked task to the status you configured.

## Features

- **Create a Jaga task from a Sentry issue**, with the attributes of the chosen task type. Jaga
  uses an EAV model — the fields depend on the pair "space + task type" — so the create form is
  built dynamically and redrawn as you pick a space and a type. The space and type last filed into
  are remembered per Sentry project. Every task is labelled (`sentry` by default), so the whole lot
  is one filter away in Jaga; clear the setting to file tasks without a label.
- **Link an existing Jaga task**: type part of a code or a title and the task is found across every
  space at once. A comment linking back to the Sentry issue is posted on the task; the text is
  pre-filled in the link form, and you can reword it or clear it to post nothing.
- **Attach the Sentry event to the task** as a JSON file, run through Sentry's own data scrubber
  first. Off by default — see [Sync settings](#sync-settings).
- **Status sync.** Resolving a Sentry issue moves the linked task to the status you chose (and
  reopening it moves the task back); a comment can be posted on top.
- **Comment sync.** Notes written on a Sentry issue can be posted as comments on the linked task,
  attributed to their author; editing the note rewrites the comment it created. Off by default.
- **Assignee sync.** Assigning a Sentry issue puts the person on the linked Jaga task; unassigning
  takes them off. The two systems are matched by email. Off by default.
- **Alert rules.** "Create a Jaga task in ... with these ..." is available as an action on an issue
  alert rule: pick the space and the task type once, and every issue that trips the rule files a
  task, linked to the Sentry issue exactly as a hand-created one is.

## Compatibility

| sentry-jaga | Sentry   | Python |
|-------------|----------|--------|
| 1.0.x       | 26.3.x   | ≥ 3.13 |

## Installation

1. Install the package into the environment of your Sentry (into the image, or into the
   virtualenv the `web` and `taskworker` processes run from):

   ```bash
   pip install sentry-jaga
   ```

2. Register the provider in `sentry.conf.py`:

   ```python
   SENTRY_DEFAULT_INTEGRATIONS = (
       *SENTRY_DEFAULT_INTEGRATIONS,
       "sentry_jaga.integration.JagaIntegrationProvider",
   )
   ```

3. *(Optional)* Turn on the live task search when linking an issue:

   ```python
   ROOT_URLCONF = "sentry_jaga.urlconf"
   ```

   `sentry_jaga.urlconf` is Sentry's own urlconf with the package's routes stacked on top —
   nothing of Sentry's is removed or shadowed. With the line, the "Task" field of the link form is
   a real autocomplete; without it, the field falls back to re-fetching the form as you type.
   Nothing breaks without it — see [Limitations](#limitations).

4. Restart Sentry — the `web` process **and the one that runs background tasks**.

   The status sync runs as a background task, so it will silently do nothing if only `web` is
   restarted. Note that Sentry 26.3 no longer uses Celery: `sentry run worker` has been removed in
   favour of `sentry run taskworker`.

## Configuration

Organization Settings → Integrations → **Jaga** → Install. In the form, enter the Jaga URL and the
email and password of a service account. A test login is performed during installation, so invalid
credentials show up right away.

The credentials are stored in Sentry's encrypted `Integration.metadata` field. Create a **dedicated
service account** with access only to the spaces you need: every task and comment is created on its
behalf.

### Sync settings

Organization Settings → Integrations → **Jaga** → Configure:

| Setting | Default | What it does |
| --- | --- | --- |
| Sync status to Jaga | on | Turns the whole status sync on or off. |
| Status to move the task to when the Sentry issue is resolved | Done | The status **category** a resolve moves the task into. |
| Status to move the task to when the Sentry issue is reopened | To do | The same, for a regression. |
| Also comment on the task | on | Post a comment in addition to moving the task. A comment is posted regardless whenever the task could not be moved. |
| Sync Sentry comments to Jaga | **off** | Post notes written on a Sentry issue as comments on the linked Jaga task, attributed to their author. |
| Sync assignment to Jaga | **off** | Put the Sentry issue's assignee on the linked Jaga task, matched by email; unassigning takes them off it. |
| Label to put on tasks created from Sentry | `sentry` | Every task the integration files carries this label. Empty box = no label. |
| Attach the Sentry event to the task | **off** | Attach the JSON of the issue's latest event to the task, as a file. |

**The event attachment is off by default because an event carries personal data** — the user's
email, the request body and headers, cookies — and a Jaga task can have a much wider audience than
a Sentry issue.

**Assignee sync is off by default because it names a real person in another system**, and Sentry's
idea of who owns an issue is not automatically the right answer inside Jaga.

**Comment sync is off by default because a Sentry note is internal discussion** — it can name a
customer, a credential or a suspect commit — and forwarding it should be an admin's decision. (The
comment posted when you *link* a task is a separate thing and needs no setting: clear the box in
the link form to post nothing.)

The three status categories — **Done**, **In progress**, **To do** — are the categories Jaga groups
all of its statuses under; one setting covers every space (see [How it works](#how-it-works)). They
are constants in the code rather than fetched from Jaga, so that the settings page renders even
when Jaga is unreachable — which is exactly when you may need to switch the sync off.

## How it works

The integration talks to Jaga's REST API as the service account: a lazy login
(`POST /v1/auth/login`), a token renewal on expiry (`POST /v1/auth/refresh`), and the token is
cached in Sentry's Django cache.

### Create

The form is built as a cascade: spaces (`GET /v1/project/list/my`, cached for 60 seconds) → task
types (`GET /v1/project/{projectId}/taskType`) → the attributes of the chosen type
(`GET /v1/project/{projectId}/taskType/{taskTypeId}`), rendered as form fields. Submitting creates
the task through `POST /v1/task/createByTaskType/{projectId}/{taskTypeId}`.

An attribute is offered only if the plugin can list its values:

| Field | Rendered as | Values from |
| --- | --- | --- |
| Title (`task.task_title`) | text, pre-filled with the Sentry issue title | — |
| Description (`task.content`) | textarea, pre-filled with the Sentry context | — |
| Any attribute with a dictionary | select | `GET /v1/listRef/{dictionaryId}/any` |
| Assignees (`task.assignee_uuid`) | multi-select | `GET /v1/team/userRoles/applications/JAGA/projects/{projectId}` — the members of the space (groups are left out) |
| Label (`task.label_id`) | multi-select | `POST /v1/labels/getPage` |

**The space and the task type are submitted inside `attributes` as well**, even though both ids are
already in the URL: Jaga rejects a create without them. They are not shown as attributes — the
cascade selects above already ask for them.

**The assignee select carries emails, not the UUIDs Jaga stores.** A person's `personUuid` — the
only thing `task.assignee_uuid` accepts — is only available from
`POST /v1/team/userProfile/findByMailOrName`, which resolves one person per call, so filling a
select of *N* members with UUIDs would cost *N* HTTP calls before the form could be drawn. The
email travels in the form instead, and the UUID is fetched at submit for whoever was actually
picked — one call, cached for an hour.

**The label** is resolved from its name through `POST /v1/labels/list`, which is a get-or-create.
The id is *merged* into the `Label` cell, so labels you picked in the form yourself are kept. A task
type with no label attribute is filed without a label.

### Link

A task is searched across all spaces at once (`POST /v1/globalSearch/findTaskList`, from 3
characters), then resolved by code (`GET /v1/task/findExtendedWithFlexField/code/{taskCode}`). The
link form has no space select: Jaga's per-space search (`searchByTitleCode`) requires a `projectId`,
the global one does not.

With `ROOT_URLCONF` set (step 3 of the installation), the "Task" field is an autocomplete backed by
the package's own endpoint (`/extensions/jaga/search/<org>/<integration_id>/`), which Sentry calls,
debounced, as you type. Without it, the field re-fetches the whole form on every keystroke.

### The event attachment

With the toggle on, the issue's latest event is serialized with `Event.as_dict()` and uploaded as
`sentry-event-<event id>.json` (`POST /v1/attacher/file/create?projectId=…&taskId=…`, multipart).

Before it is uploaded, the event goes through `sentry.relay.datascrubbing.scrub_data` — Sentry's own
scrubber, the same Relay engine that cleans an event as it *arrives*. The file therefore honours
every privacy setting of the project and the organization: IP scrubbing, additional sensitive
fields, the default data-scrubber rules, and the advanced PII rules (`sentry:relay_pii_config`).
What the scrubber does not remove still travels — by default that includes the user's email and the
request body; if you need it gone, say so in the project's privacy settings.

Because the rules are applied to the *stored* event, at export time, this is stricter than Sentry's
own "JSON" view of an event, which shows events that were stored before a setting was turned on
exactly as they were ingested. What comes back is Relay's canonical form, so it is not byte-for-byte
the `as_dict()` that went in: null keys are dropped, `message` is folded into `logentry`, and a
`_meta` block records what was scrubbed.

The upload happens after the task is created, and a failure is logged and swallowed — the task
exists by then. A failing *scrub*, on the other hand, means no attachment at all: it is fail-closed,
and an unscrubbed event is never the fallback.

The issue reaches the create through a hidden form field (`sentry_group_id`), because Sentry hands
`create_issue()` the submitted form and nothing else. The field is only emitted when the form is
opened from an issue — which is why alert rules get no attachment, see [Limitations](#limitations).

### Status sync

When a Sentry issue is resolved or reopened, the linked task is moved to another status
(`POST /v1/task/updateTaskStatusAndFields`), and a comment is posted as well (`POST /v1/comment`) if
you leave the comment option on.

**The mapping is by status category, not by status id.** Jaga gives every workflow its own copies of
the statuses — ~90k across ~15k workflows on the instance this was built against — so an id picked
in one space is meaningless in another. What is stable is the category every status belongs to
(`categoryNameM`): To do, In progress, Done. You map onto a category once, and the concrete status
is resolved per task:

1. the task is read (`GET /v1/task/findExtendedWithFlexField/code/{taskCode}`) — it reports both the
   space it lives in and the statuses its workflow can reach from where it stands
   (`statusTransitions`);
2. the statuses of that space are listed (`GET /v1/workflowStatusesAvail?projectId={spaceId}`);
3. the task is moved to the first reachable status in the chosen category.

If the workflow has no way into that category, the task is left alone and a comment is posted
instead (always, regardless of the comment setting), with a `no_status_in_category` warning in the
Sentry logs. The task is never pushed into a status its workflow does not allow.

### Assignee sync

When a Sentry issue is assigned, the person is put on the linked task by patching its assignee
attribute (`PATCH /v1/task/{taskId}` with the `task.assignee_uuid` cell). Unassigning writes an
empty list, which is how Jaga is told "nobody".

**The assignee is an ordinary EAV attribute, not a "task role"**, whatever the API spec suggests:
the documented `PUT /v1/taskRole/task/{taskId}/executor` does not exist on a live instance (404,
`No static resource ...`). Writing the attribute fills the task's `executors` as a consequence — it
*is* what Jaga's UI shows as the executor.

Sentry and Jaga are matched by email: the user's primary address first, then their verified ones.
The primary address leads because `RpcUser.emails` holds only *verified* addresses, and on a
self-hosted Sentry with no working SMTP nobody has any.

A Sentry user who does not exist in Jaga leaves the task exactly as it was: it is reported to Sentry
as a target that could not be found (and logged as `jaga.sync.assignee_not_in_jaga`), never turned
into an unassignment. Assigning an issue to a **team** does nothing in Jaga — Sentry does not queue
the outbound sync for a team at all. Unlike the status sync, a Jaga failure here is not swallowed:
Sentry retries the assignment, and swallowing would throw the retry away and record an assignment
that never happened as a success.

The issue ↔ task relation is held by Sentry's own models (`ExternalIssue` and `GroupLink`) — the
package creates no tables of its own.

## Limitations

- **Not every task field can be filled from Sentry.** Priority, version, parent, total estimate,
  deadline, and the planned/actual work periods are left out of the create form: Jaga exposes them
  with no endpoint to list their values from. Create the task from Sentry, then fill those fields in
  Jaga. If a task type marks one of them as **required**, the create fails with Jaga's own message
  and the plugin logs a `required_attribute_not_supported` warning.
- **The event attachment does not work for tasks filed by an alert rule.** The rule modal renders
  the create form with no issue behind it and saves whatever the form returns into the rule, so the
  hidden field that carries the issue is deliberately not emitted there — a group id frozen into a
  rule would attach one long-dead issue's event to every task the rule ever files. Tasks created
  from an issue by hand are unaffected.
- **An alert rule whose assignee later leaves Jaga stops filing tasks.** A rule saves the create form
  it was written with, and the assignee is saved in it as an email; if that person is removed from
  Jaga the email no longer resolves, and the rule fails rather than filing a task without an
  assignee. It is logged (`jaga.issue.assignee_not_in_jaga`), but nobody is watching the rule. Leave
  the rule's assignee empty, or check it when someone leaves.
- **A rule's saved form carries the email addresses of the space's members.** They are the choices of
  the assignee select, and Sentry stores a rule's rendered form fields on the `Rule` row — so anyone
  who can open the rule can read them.
- **Only the members of a space can be assigned, and only if the service account can see them.** The
  assignee select is filled from the space's user-role matrix
  (`GET /v1/team/userRoles/applications/JAGA/projects/{projectId}`), with `applicationMnemo=JAGA`.
  If it comes up empty, the service account cannot read that space's membership — check its rights
  in Jaga. The endpoint the spec documents for this, `GET /v1/project/getUserProfileDtos/{projectId}`,
  is dead: it answers `200 []` for every space, including one the asking account owns. It is kept
  only as a fallback for when the matrix itself errors.
- **The sync is one-way, Sentry → Jaga.** Inbound webhooks are not supported: someone reassigned in
  Jaga is not reassigned in Sentry.
- **The link suggestions do not show which space a task is in.** They read `code — title`: Jaga's
  global search returns a found task's `projectId`, `projectCode` and `projectTitle` as `null`, and
  reading them back would cost one extra request per suggestion, per keystroke. The code prefix
  usually names the space anyway (`PLT-500`).
- **The live search when linking needs one line of config.** Autocomplete requires an HTTP endpoint,
  and Sentry offers an out-of-tree package no hook to add a route with; the package ships a
  `ROOT_URLCONF` you can point Sentry at (step 3 of the installation). Leave it out and the link
  form still works, it just re-fetches the form as you type.
- **Alert-rule actions do not reach Sentry's new workflow engine.** The rule registers, saves and
  fires on the legacy path. But Sentry's new internal `Action` model is closed to out-of-tree
  integrations — its provider list and translator table are hardcoded enums with no `jaga` in them —
  so saving a Jaga rule logs one harmless `Action translator not found` error, which will need
  upstream support if Sentry ever drops the legacy path.

## Development

The package manager is [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # Python 3.13 environment + dev dependencies
uv run pytest tests/unit      # core tests — sentry is NOT needed
uv run ruff check . && uv run mypy
```

The tests in `tests/integration/` exercise the integration against a real Sentry. They skip
themselves here: Sentry is **not installable as a package** (not on PyPI above 23.7.1, and its
source tree has no build backend), so they run inside Sentry's own environment — see
[CONTRIBUTING.md](CONTRIBUTING.md#tests-of-the-sentry-layer).

How to submit changes: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
