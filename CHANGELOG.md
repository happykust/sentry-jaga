# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-07-14

The first public release. Everything below was verified against a live Jaga instance and a real
self-hosted Sentry 26.3.1, not against the API spec, which turned out to be wrong about six separate
things.

### Fixed

- **The "Assignees" select was always empty, and said nothing about it.** It was filled from the
  endpoint the spec documents for exactly this — `GET /v1/project/getUserProfileDtos/{projectId}` —
  which on a live instance answers `200 []` for *every* space, including one the asking account
  owns. It does not fail; it silently reports that nobody is there.

  The members now come from the space's user-role matrix
  (`GET /v1/team/userRoles/applications/JAGA/projects/{projectId}`). `applicationMnemo` is a required
  path segment the spec never gives a value for; `JAGA` is the one a live instance accepts. The old
  endpoint is kept only as a fallback for when the matrix itself errors — an *empty* matrix is taken
  at face value, since a space really can have no members.

### Added

- **Assignee sync (`Sync assignment to Jaga`, off by default).** Assigning a Sentry issue puts the
  person on the linked Jaga task; unassigning takes them off. Matched by email — the user's primary
  address first, then their verified ones. A Sentry user Jaga has never heard of leaves the task
  exactly as it was, and is never turned into an unassignment. Assigning an issue to a Sentry
  **team** does nothing in Jaga. Off by default because it names a real person in another system.

  The assignee turned out **not** to be the "task role" the spec advertises: the documented
  `PUT /v1/taskRole/task/{taskId}/executor` does not exist on a live instance (404, `No static
  resource ...`). It is an ordinary EAV attribute, written with `PATCH /v1/task/{taskId}`, and
  writing it fills the task's `executors` — the attribute *is* what Jaga's UI calls the executor.
- **Linking searches every space at once** (`POST /v1/globalSearch/findTaskList`). Jaga's per-space
  search demands a `projectId`, which is why the link form used to make you pick a space first.
- **The Sentry event can be attached to the task** as a JSON file (`Attach the Sentry event to the
  task`, **off by default**). It is the event as `Event.as_dict()` serves it, so it carries personal
  data — the user's email, request headers and body — which is why an admin has to turn it on
  deliberately. A failed upload is logged and swallowed: the task is already created by then.

  The attachment is run through **Sentry's own data scrubber**
  (`sentry.relay.datascrubbing.scrub_data`) before it is uploaded, so it honours every privacy
  setting of the project and the organization: IP scrubbing, additional sensitive fields, the default
  rules, and the advanced PII rules. Because the rules are applied to the *stored* event rather than
  on ingest, this is stricter than Sentry's own "JSON" view. A scrub that fails means no attachment
  at all — never an unscrubbed one.

  It does **not** apply to tasks filed by an alert rule: the rule modal renders the create form with
  no issue behind it and saves the result into the rule, so the hidden field that carries the issue
  is not emitted there — otherwise one frozen group id would attach the event of a single long-dead
  issue to every task the rule ever filed.
- **Every task created from Sentry is labelled** (`sentry` by default, configurable per
  organization; an empty setting turns it off). The label is created on first use
  (`POST /v1/labels/list` is a get-or-create) and is *added* to the labels picked in the create form
  rather than replacing them. A task type without a label attribute is filed without a label.
- The status sync now **moves the linked Jaga task**, instead of only commenting on it. The target is
  configured per organization as a status *category* (Done / In progress / To do), and the concrete
  status is resolved per task from the ones its own workflow can reach — Jaga keeps a separate copy
  of every status per workflow (~90k of them on a real instance), so a status id cannot be mapped
  directly.
- Organization settings for the sync: the categories to move the task to when an issue is resolved
  and when it regresses, and whether to comment in addition to moving.
- **The create form remembers the space and task type** last filed into, per Sentry project. A
  remembered space that Jaga no longer offers the service account falls back to the first available
  one instead of breaking the form.
- **Sentry notes can be synced to Jaga as comments** (`Sync Sentry comments to Jaga`, **off by
  default**). A note is posted as `<Author> wrote:` followed by its quoted text, and editing the note
  rewrites the comment it created rather than adding a second one. Off by default because a Sentry
  note is internal discussion and the Jaga task may have a wider audience.
- **Linking an existing task now comments on it**, with a link back to the Sentry issue. The text is
  pre-filled in the link form and is editable: clearing the box posts nothing, which is why this
  needs no organization-wide setting.

### Changed

- **The link form no longer asks for a space** (the search is global now), and its suggestions read
  `code — title`: the global search returns a found task's space as `null`, and fetching it per
  suggestion, per keystroke, is not worth the round trips. The search endpoint
  (`/extensions/jaga/search/…`) no longer takes a `project` parameter.
- The project is now fully in English: documentation, code comments and user-facing strings.
- The tests of the Sentry layer now run against a real Sentry 26.3.1, in Sentry's own environment
  (see CONTRIBUTING.md). The `sentry` dependency group is gone: it could never have worked, as Sentry
  is not installable as a package.

### The rest of what 1.0.0 contains

- Creating Jaga tasks from a Sentry issue, with the attributes of the chosen task type rendered
  dynamically (Jaga's EAV model).
- Linking an existing Jaga task to a Sentry issue.
- Installation through a form: the Jaga URL plus a service account, with the credentials checked
  before the integration is created.
- Ticket Rules: an alert rule can file a Jaga task by itself.

[Unreleased]: https://github.com/happykust/sentry-jaga/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/happykust/sentry-jaga/releases/tag/v1.0.0
