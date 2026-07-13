import tomllib
from importlib.metadata import version
from pathlib import Path

import sentry_jaga

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_package_exposes_version() -> None:
    """`__version__` is not a separate literal but the version of the installed dist."""
    assert sentry_jaga.__version__ == version("sentry-jaga")


def test_version_has_single_source_of_truth() -> None:
    """The distribution metadata must agree with `[project] version` in pyproject.

    This holds the invariant "the version is declared in exactly one place": a hardcoded
    value in `__init__.py` or in the metadata that drifted from pyproject surfaces here.
    """
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert sentry_jaga.__version__ == declared
