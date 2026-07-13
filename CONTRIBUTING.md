# Contributing

Thanks for your interest in the project! Bug reports, ideas and pull requests are welcome.

By taking part in the project you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Local development

You need [uv](https://docs.astral.sh/uv/) and Python 3.13 (uv will install it for you).

```bash
git clone https://github.com/happykust/sentry-jaga
cd sentry-jaga
uv sync                  # environment + dev dependencies
uv run pre-commit install
```

`pre-commit` runs ruff and ruff-format on every commit.

## Tests

The core tests do not need Sentry — they are enough for most changes:

```bash
uv run pytest tests/unit
```

The tests of the Sentry layer need Sentry itself. It is not on PyPI (the `sentry` package
there is frozen at 23.7.1), so it is installed from source as a separate group — that is
slow (~157 dependencies), but you only have to do it once:

```bash
uv sync --group sentry
uv run pytest tests/integration
```

Without that group, the tests in `tests/integration/` are skipped automatically, so
`uv run pytest` is green even without Sentry.

The core (the Jaga client, the field mapping) must not import `sentry` — an isolation test
checks that. Everything that knows about Sentry lives in the integration layer.

## Style

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

- ruff for linting and formatting, line length 100.
- mypy in `strict` mode — new code must be fully typed.
- Commit messages in English, on a single line, with no body
  (for example: `fix: refresh token before expiry`).

The whole project is in English: documentation, code comments, and user-facing strings
(field labels in the UI, error messages).

## Pull request

1. Fork the repository and branch off `main`.
2. Add tests for any behaviour change.
3. Make sure `ruff`, `mypy` and `pytest` are green.
4. Update `CHANGELOG.md` — the `[Unreleased]` section.
5. Open a PR and fill in the checklist from the template. Describe what changes and why; if
   you are fixing a bug, include the steps to reproduce it.

CI runs the linters, the type checks and the tests on every PR.

## Type checking against the Sentry API

By default `uv run mypy` does not see `sentry` (the package is not on PyPI), so all of its
types decay to `Any` — a typo in the name of a Sentry method would not be caught.

To check the integration layer against the **real** Sentry 26.3.1 API, give mypy the Sentry
sources (you do not need to install it — the code is enough):

```bash
git clone --depth 1 --branch 26.3.1 https://github.com/getsentry/sentry.git .sentry-src
MYPYPATH=.sentry-src/src uv run mypy --follow-imports=silent
```

That is exactly what the blocking "Types against the Sentry API" CI job does. It is the main
guarantee of correctness for the `integration.py`, `issues.py`, `sync.py`, `pipeline.py` and
`metadata.py` modules: they cannot be covered by tests, because Sentry's test stack
(Postgres/Redis/Kafka/Snuba) is out of reach in the plugin's CI.

## Release

See [docs/release.md](docs/release.md).
