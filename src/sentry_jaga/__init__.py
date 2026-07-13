"""Интеграция Sentry с таск-трекером Яга."""

from importlib.metadata import version

# Единственный источник версии — `[project] version` в pyproject.toml, откуда её
# берут метаданные установленного дистрибутива. Дублировать литерал здесь значит
# гарантированно однажды забыть его обновить.
__version__ = version("sentry-jaga")
