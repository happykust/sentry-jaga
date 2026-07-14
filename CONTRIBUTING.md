# Contributing

Bug reports, ideas and pull requests are welcome. By taking part in the project you agree to abide
by the [Code of Conduct](CODE_OF_CONDUCT.md).

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

The core (the Jaga client, the field mapping) must not import `sentry` — an isolation test checks
that. Everything that knows about Sentry lives in the integration layer.

## Tests of the Sentry layer

These exercise the integration against a **real Sentry 26.3.1**. Sentry cannot be added to this
project's environment: it is not on PyPI above 23.7.1, and its source tree is not installable either
(no `[build-system]`, `version = "0.0.0"`, and it installs itself with `tools/fast_editable.py`). So
the tests run the other way round: inside **Sentry's own environment**, with Sentry's pytest pointed
at our test directory.

You need a checkout of Sentry at the supported tag and Docker. Set the two paths once:

```bash
export JAGA=$PWD
git clone --depth 1 --branch 26.3.1 https://github.com/getsentry/sentry.git ../sentry
export SENTRY=$(cd ../sentry && pwd)
```

**1. Infrastructure** — four containers: Postgres and Redis (Sentry does not boot without them) plus
Snuba and its ClickHouse, because `get_create_issue_config()` pre-fills the description from the
issue's latest event and `Group.get_latest_event()` has exactly one backend, the Snuba event store.
No Kafka/Relay/Symbolicator: that is the event *ingest* path, and these tests never ingest an event.

```bash
docker compose -f "$JAGA/docker-compose.test.yml" up -d --wait
```

**2. Sentry's environment.** Export its lock to plain pins and install them — run `uv pip` from a
neutral directory (`/tmp`), because inside the checkout it would pick up Sentry's `[tool.uv]` block,
which is tuned for Sentry's own image build.

```bash
cd "$SENTRY" && uv venv --python 3.13 .venv
uv export --frozen --no-hashes --no-emit-project --group dev -o /tmp/sentry-reqs.txt

cd /tmp
uv pip install --python "$SENTRY/.venv/bin/python" \
  --index-url https://pypi.org/simple \
  --extra-index-url https://pypi.devinfra.sentry.io/simple \
  --index-strategy unsafe-best-match \
  -r /tmp/sentry-reqs.txt

# lxml and xmlsec each bundle their OWN copy of libxml2, and `import xmlsec` dies with
# "lxml & xmlsec libxml2 library version mismatch" whenever the two copies disagree. Sentry's
# SAML2 provider imports xmlsec during django.setup(), so when they do, NOTHING boots.
#
# Bumping xmlsec is not the fix — it only changes which two copies you get, and whether they
# agree is luck (they did on the machine this was written on; they did not on a GitHub runner).
# Build both from source instead: they then link the one libxml2 the machine actually has, and
# there is nothing left for them to disagree about.
sudo apt-get install -y libxml2-dev libxslt1-dev libxmlsec1-dev libxmlsec1-openssl pkg-config
LXML_VERSION=$("$SENTRY/.venv/bin/python" -c 'import importlib.metadata as m; print(m.version("lxml"))')
uv pip install --python "$SENTRY/.venv/bin/python" \
  --no-binary lxml --no-binary xmlsec \
  --reinstall-package lxml --reinstall-package xmlsec \
  "lxml==$LXML_VERSION" 'xmlsec>=1.3.16'
"$SENTRY/.venv/bin/python" -c 'import xmlsec'   # fails loudly here rather than in 68 tests

# Sentry, editable, with its own tool — `uv pip install -e "$SENTRY"` does not work.
cd "$SENTRY" && .venv/bin/python tools/fast_editable.py --path .

# ...and our package, editable, into that same environment.
cd /tmp && uv pip install --python "$SENTRY/.venv/bin/python" -e "$JAGA"
```

**3. Run.** Every flag is load-bearing: `PYTHONPATH` because Sentry puts its repo-root `fixtures`
package into `INSTALLED_APPS`; `-c` to take Sentry's pytest config (it carries `--nomigrations`);
`-p` to load Sentry's pytest plugin, which configures Django and the silo databases — it normally
comes from Sentry's `tests/conftest.py`, which we cannot load, because both repositories have a
top-level `tests` package and Sentry's would shadow ours.

```bash
cd "$SENTRY"
PYTHONPATH=$SENTRY .venv/bin/pytest \
  -c "$SENTRY/pyproject.toml" -p sentry.testutils.pytest "$JAGA/tests/integration"
```

Expect **68 passed** (~2 min, plus a one-off test-DB creation). Nothing in the Sentry checkout is
modified. `tests/integration/conftest.py` explains what the harness arranges itself: registering the
provider in Sentry's integration manager (in production that is the `SENTRY_DEFAULT_INTEGRATIONS`
line) and re-creating the autouse fixtures of Sentry's root conftest.

Two things about this environment are easy to get wrong:

- **Editable is not enough for entry points.** Code edits are picked up straight away, but the
  `[project.entry-points]` table is *metadata*, baked into the `.dist-info` at install time. Change
  it and reinstall — `"$SENTRY/.venv/bin/pip" install -e "$JAGA" --no-deps`. The `sentry.apps` entry
  point is what puts the package into `INSTALLED_APPS`, which runs `JagaAppConfig.ready()`, which
  registers the alert-rule action.
- **The search endpoint only exists under our urlconf.** `ROOT_URLCONF = "sentry_jaga.urlconf"` is
  optional in production, so the tests do not set it globally; `tests/integration/test_search.py`
  turns it on per-test with `override_settings`, which lets the same file assert the autocomplete
  *and* the fallback that must survive without the setting.

The same recipe runs in CI, in the `integration` job, and it blocks a merge.

## UI stand

A local Sentry 26.3.1 **with its web UI**, with `sentry-jaga` installed into it, for checking the
integration by hand in a browser.

It exists because Sentry's React frontend, not this package, renders the install form, the
Space → Task type → attributes cascade, the Ticket Rules modal, the search autocomplete and the
settings screen — we only hand Sentry the dictionaries that describe the fields. The unit tests
cover the dictionaries and the Sentry-layer tests cover the contracts; neither can tell you that a
field is mislabelled or that the cascade does not repopulate when the space changes.

**Bring it up.** The stand supplements `docker-compose.test.yml` rather than replacing it — it
reuses that project's Postgres, Redis, ClickHouse and Snuba over a shared network, so that one comes
up first.

```bash
rm -rf dist && uv build                                   # the image installs dist/*.whl
docker compose -f docker-compose.test.yml up -d --wait    # postgres, redis, clickhouse, snuba
docker compose -f docker-compose.ui.yml up -d --build     # web, worker, kafka, taskbroker

# Migrations and a superuser. `--no-deps` because neither needs the async task path — without
# it `run` would boot kafka + taskbroker just to have them sit idle.
docker compose -f docker-compose.ui.yml run --rm --no-deps web upgrade --noinput
docker compose -f docker-compose.ui.yml run --rm --no-deps web createuser \
    --email admin@example.com --password admin --superuser --no-input

# An organization, a project and one issue to file a task from. The Group is created through
# the ORM, exactly as the integration tests do it, so the stand needs no event ingest.
docker cp scripts/seed_ui_stand.py sentry-jaga-web:/seed.py
docker compose -f docker-compose.ui.yml exec -T web sentry exec /seed.py
```

Then <http://localhost:9000>, `admin@example.com` / `admin`. Worth a look:

- **Settings → Integrations** — the catalogue entry and the install form.
- **The issue page** the seed script prints a link to — *Link Jaga Task* and *Create Jaga Task*,
  where the search autocomplete and the field cascade get exercised.
- **Alerts → Create Alert Rule** — the Ticket Rules action, which renders the same create form with
  no issue behind it.

**The image installs a built wheel**, so editing the source changes nothing until you rebuild both
it and the image:

```bash
rm -rf dist && uv build
docker compose -f docker-compose.ui.yml build --no-cache web
docker compose -f docker-compose.ui.yml up -d web worker
```

`rm -rf dist` matters: `uv build` does not clean the directory, the Dockerfile globs `dist/*.whl`,
and two versions sitting there make the build fail with `ResolutionImpossible`.

**Tear it down** in this order — the network belongs to the test project, so the UI project has to
let go of it first:

```bash
docker compose -f docker-compose.ui.yml down -v
docker compose -f docker-compose.test.yml down -v
```

**This is a local stand and nothing else.** The secret key is a fixed throwaway value committed in
plain text in `docker/sentry/config.yml`, the superuser's password is `admin`, mail goes nowhere and
every port is published on `127.0.0.1` only. None of it may be reused anywhere real.

## Style

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

- ruff for linting and formatting, line length 100.
- mypy in `strict` mode — new code must be fully typed.
- Commit messages in English, on a single line, with no body
  (for example: `fix: refresh token before expiry`).

The whole project is in English: documentation, code comments, and user-facing strings.

The one exception is text quoted from Jaga verbatim — its error messages and the names of its
entities. **Do not translate those.** They are kept byte-for-byte identical to what Jaga returns, so
that someone who meets `Поле "Пространство" обязательно для заполнения` in the Sentry logs can grep
the codebase for it and land on the explanation.

## Pull request

1. Fork the repository and branch off `main`.
2. Add tests for any behaviour change.
3. Make sure `ruff`, `mypy` and `pytest` are green.
4. Update `CHANGELOG.md` — the `[Unreleased]` section.
5. Open a PR and fill in the checklist from the template.

CI runs the linters, the type checks and the tests on every PR.

## Type checking against the Sentry API

By default `uv run mypy` does not see `sentry` (the package is not on PyPI), so all of its types
decay to `Any` — a typo in the name of a Sentry method would not be caught.

To check the integration layer against the **real** Sentry 26.3.1 API, give mypy the Sentry sources
(you do not need to install it — the code is enough):

```bash
git clone --depth 1 --branch 26.3.1 https://github.com/getsentry/sentry.git .sentry-src
MYPYPATH=.sentry-src/src uv run mypy --follow-imports=silent
```

That is what the blocking "Types against the Sentry API" CI job does. `integration.py`, `issues.py`,
`sync.py`, `pipeline.py` and `metadata.py` import `sentry` at module level, so they are outside the
unit run (and out of its coverage denominator) — mypy against the real sources is the only check on
them that runs on every PR.

## Release

Releases are cut by the maintainer: pushing a `v*` tag triggers `.github/workflows/release.yml`,
which builds the distributions and publishes them to PyPI through trusted publishing (OIDC — no
tokens are stored in this repository). Contributors just add their entry to the `[Unreleased]`
section of `CHANGELOG.md`.
