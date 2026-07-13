import tomllib
from importlib.metadata import version
from pathlib import Path

import sentry_jaga

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_package_exposes_version() -> None:
    """`__version__` — не отдельный литерал, а версия установленного дистрибутива."""
    assert sentry_jaga.__version__ == version("sentry-jaga")


def test_version_has_single_source_of_truth() -> None:
    """Метаданные дистрибутива обязаны сходиться с `[project] version` в pyproject.

    Держит инвариант «версия объявлена ровно в одном месте»: хардкод в `__init__.py`
    или в метаданных, разъехавшийся с pyproject, здесь и всплывёт.
    """
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert sentry_jaga.__version__ == declared
