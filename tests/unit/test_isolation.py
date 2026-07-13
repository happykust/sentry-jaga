"""Ядро (client/, fields, descriptions) не должно импортировать sentry."""

import subprocess
import sys

# Канонический список модулей ядра. Модулей, которые ещё пишутся в параллельных
# задачах, в дереве может не быть — их пропускаем, и инвариант начнёт
# проверяться для них автоматически, как только они появятся.
CORE_MODULES = [
    "sentry_jaga.client",
    "sentry_jaga.client.api",
    "sentry_jaga.client.auth",
    "sentry_jaga.client.models",
    "sentry_jaga.client.exceptions",
    "sentry_jaga.fields",
    "sentry_jaga.descriptions",
]

# Импортируем ядро в чистом процессе и смотрим, не появился ли sentry в sys.modules.
_CHILD_SOURCE = """
import importlib
import importlib.util
import sys

imported = []
for name in {modules!r}:
    if importlib.util.find_spec(name) is None:
        continue
    importlib.import_module(name)
    imported.append(name)

leaked = [m for m in sys.modules if m == "sentry" or m.startswith("sentry.")]
print(",".join(imported))
print(",".join(leaked))
"""


def test_core_modules_do_not_import_sentry() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_SOURCE.format(modules=CORE_MODULES)],
        capture_output=True,
        text=True,
        check=True,
    )
    imported_line, leaked_line = result.stdout.splitlines()[:2]
    imported = [m for m in imported_line.split(",") if m]
    leaked = [m for m in leaked_line.split(",") if m]

    # Страховка от вырождения теста: если фильтр отсеет всё, проверять будет нечего.
    assert "sentry_jaga.fields" in imported
    assert "sentry_jaga.descriptions" in imported
    assert leaked == [], f"Ядро подтянуло sentry: {leaked}"
