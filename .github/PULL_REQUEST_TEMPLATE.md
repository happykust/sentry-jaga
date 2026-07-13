## What changes

A short description of the change and why it is needed.

Closes #

## Checklist

- [ ] Tests added for the behaviour change.
- [ ] `uv run ruff check . && uv run ruff format --check .` — green.
- [ ] `uv run mypy` — green.
- [ ] `uv run pytest tests/unit` — green (for changes in the Sentry layer, also the tests of
      that layer against a real Sentry — see CONTRIBUTING.md).
- [ ] `CHANGELOG.md` updated (the `[Unreleased]` section).
- [ ] Documentation updated, if user-facing behaviour changed.

## Notes for the reviewer

What to look at first, known trade-offs, open questions.
