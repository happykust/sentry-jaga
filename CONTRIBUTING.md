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

Without Sentry installed, the tests in `tests/integration/` skip themselves
(`pytest.importorskip("sentry")`), so `uv run pytest` is green in a plain checkout.

The core (the Jaga client, the field mapping) must not import `sentry` — an isolation test
checks that. Everything that knows about Sentry lives in the integration layer.

## Tests of the Sentry layer

These exercise the integration against a **real Sentry 26.3.1**. There is no way to add
Sentry to this project's environment: it is not on PyPI above 23.7.1, and its source tree is
not installable either — no `[build-system]`, `version = "0.0.0"`, and it installs itself with
`tools/fast_editable.py` rather than a build backend. So the tests run the other way round:
inside **Sentry's own environment**, with Sentry's pytest pointed at our test directory.

You need a checkout of Sentry at the supported tag and Docker. Set the two paths once:

```bash
export JAGA=$PWD
git clone --depth 1 --branch 26.3.1 https://github.com/getsentry/sentry.git ../sentry
export SENTRY=$(cd ../sentry && pwd)
```

**1. Infrastructure** — four containers: Postgres and Redis (Sentry does not boot without
them) plus Snuba and its ClickHouse. Snuba is there because `get_create_issue_config()`
pre-fills the description from the issue's latest event, and `Group.get_latest_event()` has
exactly one backend — the Snuba event store. No Kafka/Relay/Symbolicator: that is the event
*ingest* path, and these tests never ingest an event. Details in `docker-compose.test.yml`.

```bash
docker compose -f "$JAGA/docker-compose.test.yml" up -d --wait
```

**2. Sentry's environment.** Export its lock to plain pins and install them — run `uv pip`
from a neutral directory (`/tmp`), because inside the checkout it would pick up Sentry's
`[tool.uv]` block, which is tuned for Sentry's own image build.

```bash
cd "$SENTRY" && uv venv --python 3.13 .venv
uv export --frozen --no-hashes --no-emit-project --group dev -o /tmp/sentry-reqs.txt

cd /tmp
uv pip install --python "$SENTRY/.venv/bin/python" \
  --index-url https://pypi.org/simple \
  --extra-index-url https://pypi.devinfra.sentry.io/simple \
  --index-strategy unsafe-best-match \
  -r /tmp/sentry-reqs.txt

# The lock pins xmlsec==1.3.14, whose libxml2 does not match the one lxml is built against.
# Sentry's SAML2 provider imports xmlsec during django.setup(), so with 1.3.14 nothing boots.
uv pip install --python "$SENTRY/.venv/bin/python" 'xmlsec>=1.3.16'

# Sentry, editable, with its own tool — `uv pip install -e "$SENTRY"` does not work.
cd "$SENTRY" && .venv/bin/python tools/fast_editable.py --path .

# ...and our package, editable, into that same environment.
cd /tmp && uv pip install --python "$SENTRY/.venv/bin/python" -e "$JAGA"
```

**3. Run.** Every flag is load-bearing: `PYTHONPATH` because Sentry puts its repo-root
`fixtures` package into `INSTALLED_APPS`; `-c` to take Sentry's pytest config (it carries
`--nomigrations`); `-p` to load Sentry's pytest plugin, which configures Django and the silo
databases — it normally comes from Sentry's `tests/conftest.py`, which we cannot load, because
both repositories have a top-level `tests` package and Sentry's would shadow ours.

```bash
cd "$SENTRY"
PYTHONPATH=$SENTRY .venv/bin/pytest \
  -c "$SENTRY/pyproject.toml" -p sentry.testutils.pytest "$JAGA/tests/integration"
```

Expect **16 passed** (~50 s, plus a one-off test-DB creation). Nothing in the Sentry checkout
is modified by any of this. `tests/integration/conftest.py` explains the two things the harness
has to arrange itself: registering the provider in Sentry's integration manager (in production
that is the `SENTRY_DEFAULT_INTEGRATIONS` line in `sentry.conf.py`) and re-creating the autouse
fixtures of Sentry's root conftest.

The same recipe runs in CI, in the `integration` job — see the note there on why it does not
block a merge yet.

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

The one exception is text we quote from Jaga verbatim — its error messages and the names of
its entities. **Do not translate those.** They appear in the fixture of the error-unwrapping
test and in the docstrings that explain why the space/type cells are injected, and they are
deliberately kept byte-for-byte identical to what Jaga returns: someone who meets
`Поле "Пространство" обязательно для заполнения` in the Sentry logs can grep the codebase for
it and land on the explanation. Translating them would break that link and make the test stop
checking Jaga's real response format.

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

That is exactly what the blocking "Types against the Sentry API" CI job does. Together with the
Sentry layer tests above, it is what holds `integration.py`, `issues.py`, `sync.py`,
`pipeline.py` and `metadata.py`: those modules import `sentry` at module level, so they are
outside the unit run (and out of its coverage denominator) — mypy against the real sources is
the only check on them that runs on every PR.

## Release

See [docs/release.md](docs/release.md).
