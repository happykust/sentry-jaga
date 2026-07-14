# sentry-jaga

[![CI](https://github.com/happykust/sentry-jaga/actions/workflows/ci.yml/badge.svg)](https://github.com/happykust/sentry-jaga/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sentry-jaga.svg)](https://pypi.org/project/sentry-jaga/)
[![Python](https://img.shields.io/pypi/pyversions/sentry-jaga.svg)](https://pypi.org/project/sentry-jaga/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Integration between self-hosted Sentry and **Jaga**, the issue tracker by Rostelecom.

The package adds an integration provider to Sentry: from an issue you can open a task in
Jaga or link an existing one, and resolving the issue in Sentry moves the linked task to the
status you configured (and, optionally, comments on it).

## Features

- **Create a Jaga task from a Sentry issue**, with the full set of attributes of the chosen
  task type. Jaga uses an EAV model — the set of fields depends on the pair
  "space + task type" — so the create form is built dynamically and redrawn as you pick a
  space and a type.

  The space and task type you last filed into are **remembered per Sentry project**, so a team
  that always files into one space does not have to pick it again every time.
  Every task filed from Sentry is **labelled** (`sentry` by default), so the whole lot can be
  found in Jaga with one filter. The label is created on first use; clear the setting to file
  tasks without one.
- **Link an existing Jaga task** to a Sentry issue: type part of a code or a title and the task
  is found **across every space at once** — no space to pick first. A comment linking back to the
  Sentry issue is posted on the task; the text is pre-filled in the link form, and you can reword
  it or clear it to post nothing.
- **Attach the Sentry event to the task** as a JSON file — the same payload Sentry shows behind
  the "JSON" link on an event. **Off by default**, because an event carries personal data; see
  [Sync settings](#sync-settings).
- **Status sync.** Resolving a Sentry issue **moves the linked task** to the status you chose
  (and reopening it moves the task back); a comment can be posted on top. You map onto a status
  *category* — Done / In progress / To do — and the concrete status is resolved per task from
  the ones its own workflow allows.
- **Comment sync.** Notes written on a Sentry issue can be posted as comments on the linked
  Jaga task, attributed to their author; editing the note rewrites the comment it created. Off
  by default — see [Sync settings](#sync-settings).

## Compatibility

| sentry-jaga | Sentry   | Python |
|-------------|----------|--------|
| 0.1.x       | 26.3.x   | ≥ 3.13 |

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

   This is the ordinary Django setting, and `sentry_jaga.urlconf` is Sentry's own urlconf with
   the package's routes stacked on top — nothing of Sentry's is removed or shadowed. It exists
   because Sentry has no hook for an out-of-tree package to add a route, and a search endpoint
   is what an autocomplete needs.

   **It is optional and nothing breaks without it.** With the line, the "Task" field of the link
   form becomes a real autocomplete: you type, and Sentry queries the endpoint (debounced) and
   shows the matches. Without it, the field falls back to searching by re-fetching the form as
   you type — slower and chattier, but it works. See [Limitations](#limitations).

4. Restart Sentry — the `web` process **and the one that runs background tasks**.

   The status sync (moving the Jaga task when a Sentry issue is resolved) runs as a background
   task, so it will silently do nothing if only `web` is restarted. Note that Sentry 26.3 no
   longer uses Celery: `sentry run worker` has been removed in favour of `sentry run taskworker`.

## Configuration

Organization Settings → Integrations → **Jaga** → Install. In the form, enter the Jaga URL
and the email and password of a service account. A test login is performed during
installation, so invalid credentials show up right away.

The credentials are stored in Sentry's encrypted `Integration.metadata` field. Create a
**dedicated service account** for the integration, with access only to the spaces you need
to create tasks in: every task and comment will be created on its behalf.

### Sync settings

Organization Settings → Integrations → **Jaga** → Configure:

| Setting | Default | What it does |
| --- | --- | --- |
| Sync status to Jaga | on | Turns the whole status sync on or off. |
| Status to move the task to when the Sentry issue is resolved | Done | The status **category** a resolve moves the task into. |
| Status to move the task to when the Sentry issue is reopened | To do | The same, for a regression. |
| Also comment on the task | on | Post a comment in addition to moving the task. A comment is posted regardless whenever the task could not be moved. |
| Sync Sentry comments to Jaga | **off** | Post notes written on a Sentry issue as comments on the linked Jaga task, attributed to their author. |
| Label to put on tasks created from Sentry | `sentry` | Every task the integration files carries this label. Empty box = no label. |
| Attach the Sentry event to the task | **off** | Attach the JSON of the issue's latest event to the task, as a file. |

**The event attachment is off by default on purpose — the file contains personal data.** It is
the event exactly as Sentry stores it: the user's email and IP address, the request headers and
body, cookies, and anything else your SDK sent. That is the same content Sentry shows behind the
"JSON" link on an event page, but a Jaga task can have a much wider audience than a Sentry issue,
and once a file is on a task it stays there. Turn this on only if that is acceptable — and note
that what lands in the file is what Sentry *stored*: if you rely on Sentry's data-scrubbing, scrub
at ingest (project settings → Security & Privacy), because nothing is stripped on the way out.

It does **not** apply to tasks filed by an alert rule — see [Limitations](#limitations).

**Comment sync is off by default on purpose.** A Sentry note is internal discussion — it can
name a customer, a credential or a suspect commit — and the Jaga task may have a wider audience
than the Sentry issue. Forwarding it should be a decision an admin takes, not a surprise they
discover. (Every issue integration in Sentry upstream defaults this the same way.) With it on, a
note becomes a comment as `<Author> wrote:` followed by the quoted text, and editing the note
rewrites that comment rather than adding another.

Note that the comment posted when you **link** an existing task is a separate thing and needs no
setting: the link form pre-fills it and you can clear the box to post nothing.

The two category settings offer **Done**, **In progress** and **To do** — the categories Jaga
groups all of its statuses under. One setting covers every space: the concrete status is picked
per task, from the ones its own workflow can reach (see [How it works](#how-it-works)). That is
also why these three choices are constants in the code rather than something fetched from Jaga:
the settings page has to render even when Jaga is unreachable — that is exactly when you might
need it, if only to switch the sync off.

## How it works

The integration talks to Jaga's REST API as the service account: a lazy login
(`POST /v1/auth/login`), a token renewal on expiry (`POST /v1/auth/refresh`), and the token
is cached in Sentry's Django cache.

- **Create.** The form is built as a cascade: spaces (`GET /v1/project/list/my`, the list is
  cached for 60 seconds) → task types (`GET /v1/project/{projectId}/taskType`) → the
  attributes of the chosen type (`GET /v1/project/{projectId}/taskType/{taskTypeId}`), which
  are rendered as form fields. Submitting creates the task through
  `POST /v1/task/createByTaskType/{projectId}/{taskTypeId}`.

  Which attributes the form offers is decided by what the plugin can list the values of:

  | Field | Rendered as | Values from |
  | --- | --- | --- |
  | Title (`task.task_title`) | text, pre-filled with the Sentry issue title | — |
  | Description (`task.content`) | textarea, pre-filled with the Sentry context | — |
  | Any attribute with a dictionary | select | `GET /v1/listRef/{dictionaryId}/any` |
  | Assignees (`task.assignee_uuid`) | multi-select | `GET /v1/project/getUserProfileDtos/{projectId}` (blocked and non-assignable members are filtered out) |
  | Label (`task.label_id`) | multi-select | `POST /v1/labels/getPage` |

  The space and the task type are **not** shown as attributes — the cascade selects above
  already ask for them — but they are still submitted inside `attributes`, because Jaga
  rejects a create without them even though both ids are in the URL. The author and the
  creation date are filled in by Jaga.
- **The label on every task from Sentry.** The name of the label is resolved to an id through
  `POST /v1/labels/list`, which is a get-or-create: the first task ever filed makes the label,
  every later one reuses it. The id is **merged** into the `Label` cell of the create, so labels
  you picked in the form yourself are kept — the automatic one is added to them, not put in
  their place. A task type with no label attribute is filed without a label.
- **Link.** A task is searched across **all** spaces at once
  (`POST /v1/globalSearch/findTaskList`, starting from 3 characters of the query), then resolved
  by code (`GET /v1/task/findExtendedWithFlexField/code/{taskCode}`). The link form has no space
  select: Jaga's per-space search (`searchByTitleCode`) requires a `projectId`, the global one
  does not.

  How you search depends on whether `ROOT_URLCONF` is set (step 3 of the installation). With
  it, the "Task" field is an autocomplete backed by the package's own endpoint
  (`/extensions/jaga/search/<org>/<integration_id>/`), which Sentry calls, debounced, as you
  type. Without it, the field falls back to a plain text box that re-fetches the whole form on
  every keystroke.
- **Alert rules.** "Create a Jaga task in ... with these ..." is available as an action on an
  issue alert rule: pick the space and the task type when you set the rule up, and every issue
  that trips it files a task. The title and the description come from the event itself, and the
  task is linked to the Sentry issue exactly as a hand-created one is — so the status sync
  applies to it too.
- **The event attachment.** With the toggle on, the issue's latest event is serialized with
  `Event.as_dict()` — the same normalized form Sentry itself serves behind the "JSON" link on an
  event page — and uploaded as `sentry-event-<event id>.json`
  (`POST /v1/attacher/file/create?projectId=…&taskId=…`, multipart).

  It happens **after** the task is created, and a failed upload is logged and swallowed: the task
  exists by then, and losing the create over an attachment would be a worse bug than losing the
  attachment.

  The issue reaches the create through a **hidden field** in the form (`sentry_group_id`). Sentry
  hands `create_issue()` the submitted form and nothing else — no group, no event — and there is
  no "after create, with the event" hook to use instead. The field is only emitted when the form
  is opened from an issue, which is why alert rules get no attachment: see
  [Limitations](#limitations).
- **Status sync.** When a Sentry issue is resolved or reopened, the linked task is **moved to
  another status** (`POST /v1/task/updateTaskStatusAndFields`) — and, if you leave the comment
  option on, a comment is posted as well (`POST /v1/comment`).

  The mapping is by **status category**, not by status id. Jaga gives every workflow its own
  copies of the statuses, so a single instance carries tens of thousands of them (~90k across
  ~15k workflows on the one this was built against) — far too many for a dropdown, and an id
  picked in one space is meaningless in another. What *is* stable is the category every status
  belongs to (`categoryNameM`): **To do**, **In progress**, **Done**. So you map onto a
  category once, for the whole organization, and the concrete status is resolved per task:

  1. the task is read (`GET /v1/task/findExtendedWithFlexField/code/{taskCode}`) — it reports
     both the space it lives in and the statuses its workflow can reach from where it stands
     (`statusTransitions`);
  2. the statuses of that space are listed
     (`GET /v1/workflowStatusesAvail?projectId={spaceId}`, a handful — not the global ~90k);
  3. the task is moved to the **first reachable** status in the chosen category.

  If the workflow has no way into that category from the task's current status, the task is
  **left alone** and a comment is posted instead (always — regardless of the comment setting),
  with a `no_status_in_category` warning in the Sentry logs naming the task and the statuses it
  could have reached. Nothing is forced: the task is never pushed into a status its workflow
  does not allow.

The issue ↔ task relation is held by Sentry's own models (`ExternalIssue` and `GroupLink`) —
the package creates no tables of its own.

## Limitations

- **Not every task field can be filled from Sentry.** Priority, version, parent, total
  estimate, deadline, and the planned/actual work periods are left out of the create form:
  Jaga exposes them as references or typed values with no endpoint to list them from, and a
  free-text box over an id column would only produce values Jaga rejects. Create the task
  from Sentry, then fill those fields in Jaga itself.

  If a task type marks one of them as **required**, the create will fail with Jaga's own
  message naming the field, and the plugin logs a `required_attribute_not_supported` warning
  with its mnemonic. Either make the field optional in Jaga, or create such tasks by hand.
- **The event attachment does not work for tasks filed by an alert rule.** The rule modal renders
  the create form with no issue behind it (Sentry calls `get_create_issue_config(None, …)` there)
  and **saves whatever the form returns into the rule**. So the hidden field that carries the
  issue is deliberately not emitted on that path: a group id saved into a rule would be frozen at
  the moment the rule was written, and every task the rule ever filed afterwards would get the
  event of that one long-dead issue attached to it. No field, no attachment — the honest outcome.
  Tasks created from an issue by hand are unaffected.
- **The sync is one-way, Sentry → Jaga.** Inbound Jaga → Sentry webhooks are not supported:
  changes made on the Jaga side do not reach Sentry.
- **The link suggestions do not show which space a task is in.** They read `code — title` and no
  more. Jaga's global search returns a found task's `projectId`, `projectCode` and `projectTitle`
  as `null` — the space simply is not in the answer — and reading it back would mean one extra
  request per suggestion, on every keystroke. The code prefix usually names the space anyway
  (`PLT-500`), and the task's full card is one click away in Jaga once it is linked.
- **The live search when linking needs one line of config.** Autocomplete requires an HTTP
  endpoint, and Sentry offers an out-of-tree package no hook to add a route with. The package
  ships one anyway, as a `ROOT_URLCONF` you can point Sentry at (step 3 of the installation).
  Leave it out and the link form still works — it just searches by re-fetching the form as you
  type, which is slower and chattier.
- **Alert-rule actions do not reach Sentry's new workflow engine.** The rule works: it is in
  Sentry's rule registry, it saves, and it fires. But Sentry is migrating issue alerts to a new
  internal `Action` model, and that path is closed to out-of-tree integrations — its provider
  list (`Action.Type`) and its translator table are hardcoded enums, with no `jaga` in them.
  Saving a Jaga rule therefore logs one `Action translator not found` error, which is harmless
  today (the legacy path is what executes) but will need upstream support if Sentry ever drops
  it.

## Development

The package manager is [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # Python 3.13 environment + dev dependencies
uv run pytest tests/unit      # core tests — sentry is NOT needed
uv run ruff check . && uv run mypy
```

The tests in `tests/integration/` exercise the integration against a real Sentry. They skip
themselves here, and cannot run in this environment at all: Sentry is **not installable as a
package** (not on PyPI above 23.7.1, and its source tree has no build backend). They run inside
Sentry's own environment instead — four containers and a checkout of the tag, see
[CONTRIBUTING.md](CONTRIBUTING.md#tests-of-the-sentry-layer).

How to submit changes: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
