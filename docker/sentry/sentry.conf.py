# Sentry's Python-side settings for the local UI stand (docker-compose.ui.yml).
#
# This file replaces the one baked into the official image (which is env-var driven and aimed
# at the old `sentry/onpremise` layout). Everything is spelled out literally instead: the stand
# has exactly one shape and an explicit file is easier to read than a chain of `env(...) or`.
#
# THIS IS A LOCAL STAND, NOT PRODUCTION.

from sentry.conf.server import *  # noqa: F403

# ---------------------------------------------------------------- Postgres / Redis / Snuba
#
# All three are the containers of docker-compose.test.yml, reused as-is: this compose project
# joins their network (`sentry-jaga_default`), where they answer to the DNS names below.

DATABASES = {
    "default": {
        "ENGINE": "sentry.db.postgres",
        "NAME": "sentry",
        "USER": "postgres",
        "PASSWORD": "",
        "HOST": "postgres",
        "PORT": "5432",
    }
}

SENTRY_OPTIONS["redis.clusters"] = {  # noqa: F405
    "default": {"hosts": {0: {"host": "redis", "port": 6379, "db": 0}}}
}

# `SENTRY_SNUBA` also has an env-var default (SNUBA), but pinning it here keeps the whole
# wiring in one file.
SENTRY_SNUBA = "http://snuba:1218"

SENTRY_CACHE = "sentry.cache.redis.RedisCache"
SENTRY_BUFFER = "sentry.buffer.redis.RedisBuffer"
SENTRY_QUOTAS = "sentry.quotas.redis.RedisQuota"
SENTRY_RATELIMITER = "sentry.ratelimits.redis.RedisRateLimiter"
SENTRY_DIGESTS = "sentry.digests.backends.redis.RedisBackend"
# Without this the default is DummyTSDB, and every process boots with a loud
# "not recommended for production use" warning. Counters/graphs on the issue page stay empty
# either way (nothing ingests events), but this keeps the stand's backends the same as a real
# deployment's, so the UI does not take some dummy-only code path.
SENTRY_TSDB = "sentry.tsdb.redissnuba.RedisSnubaTSDB"

# The DJANGO cache (not SENTRY_CACHE) has to be Redis rather than the default locmem, because
# `sentry_jaga.integration.DjangoCache` keeps the Jaga auth token and the list of spaces in it.
# With locmem, web and taskworker are separate processes with separate caches, so the worker
# would re-authenticate on every task and the token cache would look broken in a way that does
# not reproduce in production.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://redis:6379/1",
    }
}

# ------------------------------------------------------------------------------ Taskworker
#
# Sentry 26.3 has NO CELERY (the package is not even installed; `sentry run worker` is a stub
# that prints "use `sentry run taskworker` instead" in a loop). Async tasks — among them the
# outbound status sync of this integration — go through taskworker: the web process produces to
# a Kafka topic, `taskbroker` consumes it and hands tasks out over gRPC, `sentry run taskworker`
# executes them. Hence the kafka + taskbroker services in docker-compose.ui.yml.
#
# The DefaultRouter sends every namespace to a single topic (`taskworker`) in monolith mode,
# so one broker and one worker cover everything.
KAFKA_CLUSTERS["default"]["common"]["bootstrap.servers"] = "kafka:9092"  # noqa: F405

# ---------------------------------------------------------------------------- Web / general
SENTRY_WEB_HOST = "0.0.0.0"
SENTRY_WEB_PORT = 9000
SENTRY_WEB_OPTIONS: dict = {}

# False, not True: with a single organization Sentry hides the org switcher and some of the
# organization-level settings routes, and the integration directory is exactly an
# organization-level route. Keep the ordinary multi-org UI.
SENTRY_SINGLE_ORGANIZATION = False

# No Relay on the stand — nothing ingests events (the Group is created through the ORM, as in
# the integration tests). This is the default in sentry.conf.server anyway; it is spelled out
# because the image's own sentry.conf.py flips it to True at the very last line.
SENTRY_USE_RELAY = False

# Local stand: let anyone who reaches it register/log in without an invite dance.
SENTRY_FEATURES["auth:register"] = True  # noqa: F405
SENTRY_FEATURES["organizations:create"] = True  # noqa: F405

# --------------------------------------------------------------------- THE POINT OF ALL THIS
#
# Register the Jaga provider. The Django app (`sentry_jaga.apps.JagaAppConfig`) needs no entry
# here — it is picked up on its own through the `sentry.apps` entry point of the installed
# package.
SENTRY_DEFAULT_INTEGRATIONS = (
    *SENTRY_DEFAULT_INTEGRATIONS,  # noqa: F405
    "sentry_jaga.integration.JagaIntegrationProvider",
)

# Optional: registers sentry-jaga's own URLs (the task search endpoint), which turns the
# "link an existing task" field into a live autocomplete. Without this line the integration
# still works — the search just falls back to reloading the form.
ROOT_URLCONF = "sentry_jaga.urlconf"
