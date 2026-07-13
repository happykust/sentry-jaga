# sentry-jaga

[![CI](https://github.com/happykust/sentry-jaga/actions/workflows/ci.yml/badge.svg)](https://github.com/happykust/sentry-jaga/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sentry-jaga.svg)](https://pypi.org/project/sentry-jaga/)
[![Python](https://img.shields.io/pypi/pyversions/sentry-jaga.svg)](https://pypi.org/project/sentry-jaga/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Интеграция self-hosted Sentry с таск-трекером **Яга** (Ростелеком).

Пакет добавляет в Sentry провайдер интеграции: из карточки issue можно завести
задачу в Яге или привязать существующую, а изменение статуса issue уезжает в
задачу комментарием.

## Возможности

- **Создание задачи в Яге из Sentry-issue** с полным набором атрибутов выбранного
  типа задачи. У Яги EAV-модель — набор полей зависит от пары «пространство + тип
  задачи», поэтому форма создания строится динамически и перерисовывается по мере
  того, как вы выбираете пространство и тип.
- **Привязка существующей задачи Яги** к Sentry-issue по коду задачи, с поиском по
  названию и коду.
- **Комментарий в задачу** при закрытии и переоткрытии Sentry-issue.

## Совместимость

| sentry-jaga | Sentry   | Python |
|-------------|----------|--------|
| 0.1.x       | 26.3.x   | ≥ 3.13 |

## Установка

1. Установите пакет в окружение вашего Sentry (в образ или virtualenv, из которого
   запускаются `web` и `worker`):

   ```bash
   pip install sentry-jaga
   ```

2. Зарегистрируйте провайдер в `sentry.conf.py`:

   ```python
   SENTRY_DEFAULT_INTEGRATIONS = (
       *SENTRY_DEFAULT_INTEGRATIONS,
       "sentry_jaga.integration.JagaIntegrationProvider",
   )
   ```

3. Перезапустите Sentry — и `web`, и `worker`.

## Настройка

Organization Settings → Integrations → **Яга** → Install. В форме укажите адрес
Яги, email и пароль сервисного аккаунта. При установке выполняется пробный вход,
поэтому неверные учётные данные вы увидите сразу.

Учётные данные хранятся в зашифрованном поле `Integration.metadata` Sentry.
Заводите под интеграцию **отдельный сервисный аккаунт** с доступом только к тем
пространствам, в которых нужно создавать задачи: все задачи и комментарии будут
создаваться от его имени.

## Как это работает

Интеграция ходит в REST API Яги от имени сервисного аккаунта: ленивый вход
(`POST /v1/auth/login`), обновление токена по истечении (`POST /v1/auth/refresh`),
токен кэшируется в Django-кэше Sentry.

- **Создание.** Форма собирается каскадом: пространства (`GET /v1/project/search/my`)
  → типы задач (`GET /v1/project/{projectId}/taskType`) → атрибуты выбранного типа
  (`GET /v1/project/{projectId}/taskType/{taskTypeId}`), из которых рендерятся поля
  формы. Submit создаёт задачу через
  `POST /v1/task/createByTaskType/{projectId}/{taskTypeId}`.
- **Привязка.** Поиск задачи по названию или коду (`GET /v1/task/searchByTitleCode`),
  затем задача разрешается по коду
  (`GET /v1/task/findExtendedWithFlexField/code/{taskCode}`).
- **Синхронизация статуса.** При закрытии или переоткрытии Sentry-issue в связанную
  задачу отправляется комментарий (`POST /v1/comment`).

Связь issue ↔ задача хранят штатные модели Sentry (`ExternalIssue` и `GroupLink`) —
пакет не заводит собственных таблиц.

## Ограничения

- **Синхронизация односторонняя, Sentry → Яга.** Входящие вебхуки Яга → Sentry не
  поддерживаются: изменения на стороне Яги в Sentry не приезжают.
- **Нет live-autocomplete при линковке.** Поиск задачи выполняется через обновление
  формы, а не подсказками по мере ввода: внешний пакет не может зарегистрировать
  search-endpoint в urlconf Sentry.

## Разработка

Пакетный менеджер — [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # окружение Python 3.13 + dev-зависимости
uv run pytest tests/unit      # тесты ядра — sentry НЕ нужен
uv run ruff check . && uv run mypy
```

Тесты слоя Sentry требуют самого Sentry. **Его нет в PyPI** (пакет `sentry` там
заморожен на 23.7.1), поэтому он ставится из исходников отдельной группой:

```bash
uv sync --group sentry            # ~157 зависимостей, долго
uv run pytest tests/integration
```

Без этой группы тесты в `tests/integration/` автоматически скипаются.

Как присылать изменения — в [CONTRIBUTING.md](CONTRIBUTING.md).

## Лицензия

MIT — см. [LICENSE](LICENSE).
