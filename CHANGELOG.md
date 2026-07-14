# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The status sync now **moves the linked Jaga task**, instead of only commenting on it. The
  target is configured per organization as a status *category* (Done / In progress / To do),
  and the concrete status is resolved per task from the ones its own workflow can reach — Jaga
  keeps a separate copy of every status per workflow (~90k of them on a real instance), so a
  status id cannot be mapped directly.
- Organization settings for the sync: the categories to move the task to when an issue is
  resolved and when it regresses, and whether to comment in addition to moving.

### Changed

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
