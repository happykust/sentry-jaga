"""Sentry integration for the Jaga issue tracker."""

from importlib.metadata import version

# The single source of truth for the version is `[project] version` in pyproject.toml,
# which is where the installed distribution's metadata comes from. Duplicating the
# literal here would guarantee that one day we forget to update it.
__version__ = version("sentry-jaga")
