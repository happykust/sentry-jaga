# sentry-jaga

[![CI](https://github.com/happykust/sentry-jaga/actions/workflows/ci.yml/badge.svg)](https://github.com/happykust/sentry-jaga/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sentry-jaga.svg)](https://pypi.org/project/sentry-jaga/)
[![Python](https://img.shields.io/pypi/pyversions/sentry-jaga.svg)](https://pypi.org/project/sentry-jaga/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Integration between self-hosted Sentry and **Jaga**, the issue tracker by Rostelecom.

The package adds an integration provider to Sentry: from an issue you can open a task in
Jaga or link an existing one, and a change of the issue status is posted to the task as a
comment.

## Features

- **Create a Jaga task from a Sentry issue**, with the full set of attributes of the chosen
  task type. Jaga uses an EAV model — the set of fields depends on the pair
  "space + task type" — so the create form is built dynamically and redrawn as you pick a
  space and a type.
- **Link an existing Jaga task** to a Sentry issue by task code, with search by title and
  code.
- **A comment on the task** when a Sentry issue is resolved or reopened.

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

3. Restart Sentry — the `web` process **and the one that runs background tasks**.

   The status sync (the comment posted to the Jaga task when a Sentry issue is resolved) runs
   as a background task, so it will silently do nothing if only `web` is restarted. Note that
   Sentry 26.3 no longer uses Celery: `sentry run worker` has been removed in favour of
   `sentry run taskworker`.

## Configuration

Organization Settings → Integrations → **Jaga** → Install. In the form, enter the Jaga URL
and the email and password of a service account. A test login is performed during
installation, so invalid credentials show up right away.

The credentials are stored in Sentry's encrypted `Integration.metadata` field. Create a
**dedicated service account** for the integration, with access only to the spaces you need
to create tasks in: every task and comment will be created on its behalf.

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
- **Status sync.** When a Sentry issue is resolved or reopened, a comment is posted to the
  linked task (`POST /v1/comment`).

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
- **No live autocomplete when linking.** A task is searched by refreshing the form rather
  than by suggestions as you type: an external package cannot register a search endpoint in
  Sentry's urlconf.

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
