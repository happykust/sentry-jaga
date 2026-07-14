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
- **Link an existing Jaga task** to a Sentry issue by task code, with search by title and
  code.
- **Status sync.** Resolving a Sentry issue **moves the linked task** to the status you chose
  (and reopening it moves the task back); a comment can be posted on top. You map onto a status
  *category* — Done / In progress / To do — and the concrete status is resolved per task from
  the ones its own workflow allows.

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

### Status sync settings

Organization Settings → Integrations → **Jaga** → Configure:

| Setting | Default | What it does |
| --- | --- | --- |
| Sync status to Jaga | on | Turns the whole sync on or off. |
| Status to move the task to when the Sentry issue is resolved | Done | The status **category** a resolve moves the task into. |
| Status to move the task to when the Sentry issue is reopened | To do | The same, for a regression. |
| Also comment on the task | on | Post a comment in addition to moving the task. A comment is posted regardless whenever the task could not be moved. |

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
- **Link.** A task is searched by title or code (`GET /v1/task/searchByTitleCode`, starting
  from 3 characters of the query), then resolved by code
  (`GET /v1/task/findExtendedWithFlexField/code/{taskCode}`).

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
- **The sync is one-way, Sentry → Jaga.** Inbound Jaga → Sentry webhooks are not supported:
  changes made on the Jaga side do not reach Sentry.
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
