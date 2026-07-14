# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Linking searches every space at once.** Typing part of a code or a title now finds the task
  wherever it lives (`POST /v1/globalSearch/findTaskList`) — Jaga's per-space search demands a
  `projectId`, which is why the link form used to make you pick a space first.
- **The Sentry event can be attached to the task** as a JSON file (`Attach the Sentry event to
  the task`, **off by default**). It is the event as Sentry itself serves it behind the "JSON"
  link on an event page (`Event.as_dict()`), and it therefore carries personal data — the user's
  email, request headers and body — which is why an admin has to turn it on deliberately. A failed
  upload is logged and swallowed: the task is already created by then.

  The attachment is run through **Sentry's own data scrubber** (`sentry.relay.datascrubbing.
  scrub_data`) before it is uploaded — the same Relay engine, with the same rules, that cleans an
  event as it arrives in Sentry. It therefore honours *every* privacy setting of the project and
  the organization: IP scrubbing, additional sensitive fields, the default data-scrubbing rules,
  and the advanced PII rules an admin wrote by hand. Scrubbing of our own would only ever have
  covered the fields we thought of — an admin who told Sentry to strip `authorization` would have
  found it in plain text on a Jaga task.

  Because the rules are applied to the *stored* event rather than on ingest, this is **stricter
  than Sentry's own "JSON" view of an event**: Sentry never cleaned the events that were stored
  before a setting was turned on, and that page still shows what is in them. A page is one
  authorized person reading one event inside Sentry; this file is an export into a tracker with a
  wider audience. A scrub that fails means no attachment at all — never an unscrubbed one.

  It does **not** apply to tasks filed by an alert rule. The rule modal renders the create form
  with no issue behind it and saves the result into the rule, so the hidden field that carries the
  issue is not emitted there — otherwise a single group id would be frozen into the rule and every
  task it ever filed would carry the event of one long-dead issue.
- **Every task created from Sentry is labelled** (`sentry` by default, configurable per
  organization; an empty setting turns it off). All the tasks the integration ever filed are
  then one filter away in Jaga. The label is created on first use (`POST /v1/labels/list` is a
  get-or-create), and it is *added* to the labels picked in the create form rather than
  replacing them. A task type without a label attribute is filed without a label.

- The status sync now **moves the linked Jaga task**, instead of only commenting on it. The
  target is configured per organization as a status *category* (Done / In progress / To do),
  and the concrete status is resolved per task from the ones its own workflow can reach — Jaga
  keeps a separate copy of every status per workflow (~90k of them on a real instance), so a
  status id cannot be mapped directly.
- Organization settings for the sync: the categories to move the task to when an issue is
  resolved and when it regresses, and whether to comment in addition to moving.
- **The create form remembers the space and task type** last filed into, per Sentry project, so
  a team that always files into one space no longer starts from the first space in the list
  every time. A remembered space that Jaga no longer offers the service account — archived, or
  access revoked — falls back to the first available one instead of breaking the form.
- **Sentry notes can be synced to Jaga as comments** (`Sync Sentry comments to Jaga`,
  **off by default**). A note is posted as `<Author> wrote:` followed by its quoted text, and
  editing the note rewrites the comment it created rather than adding a second one. It is off
  by default because a Sentry note is internal discussion and the Jaga task may have a wider
  audience — the same default every issue integration in Sentry upstream uses.
- **Linking an existing task now comments on it**, with a link back to the Sentry issue. The
  text is pre-filled in the link form and is editable: clearing the box posts nothing, which is
  why this needs no organization-wide setting of its own.

### Changed

- **The link form no longer asks for a space** (the search is global now), and its suggestions
  read `code — title`: the global search returns a found task's space as `null`, and fetching it
  per suggestion, per keystroke, is not worth the round trips. The search endpoint
  (`/extensions/jaga/search/…`) no longer takes a `project` parameter.
- The project is now fully in English: documentation, code comments and user-facing strings.
- The tests of the Sentry layer now actually run against a real Sentry 26.3.1 (in Sentry's own
  environment — see CONTRIBUTING.md). The `sentry` dependency group is gone: it could never
  have worked, as Sentry is not installable as a package.

## [0.1.0] - 2026-06-25

### Added

- Creating Jaga tasks from a Sentry issue, with dynamic rendering of the task type's
  attributes.
- Linking existing Jaga tasks to a Sentry issue, with search by code and title.
- A comment on the Jaga task when a Sentry issue is resolved or reopened.
- Installation through a form: the Jaga URL plus a service account, with a credential check.

[Unreleased]: https://github.com/happykust/sentry-jaga/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/happykust/sentry-jaga/releases/tag/v0.1.0
