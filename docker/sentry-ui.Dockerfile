# Sentry with sentry-jaga installed into it — the local UI stand (docker-compose.ui.yml).
#
# The base image is the OFFICIAL Sentry image: it already carries the compiled frontend
# bundle (pnpm + rspack), which takes tens of minutes to build from source. Never build the
# frontend here.
#
# Build context is the repository root, and the wheel must exist beforehand:
#
#   uv build
#   docker compose -f docker-compose.ui.yml build
#
# AFTER EVERY CODE CHANGE the wheel has to be rebuilt AND the image rebuilt from it, or the
# container quietly keeps running the old code:
#
#   rm -rf dist && uv build
#   docker compose -f docker-compose.ui.yml build --no-cache web
FROM ghcr.io/getsentry/sentry:26.3.1

# The image ships pip (not uv) inside its own virtualenv at /.venv, already on PATH.
#
# The glob is version-agnostic on purpose: the project version changes, and a filename pinned to
# one version silently left a stale wheel in the image (0.1.0 stayed installed long after the
# project had moved to 1.0.0). The flip side is that `dist/` must hold exactly one wheel —
# `uv build` does not clean it, and two versions there make pip fail with ResolutionImpossible.
# Hence `rm -rf dist` above rather than a bare `uv build`.
COPY dist/*.whl /tmp/wheels/
RUN pip install --no-deps /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

# `--no-deps` is safe and deliberate: our only runtime dependency is `requests`, which Sentry
# itself pins and already has installed. Letting pip resolve would risk it moving Sentry's own
# pinned requests/urllib3 out from under it.
RUN python -c "import sentry_jaga, requests; print('sentry_jaga', sentry_jaga.__file__)"
