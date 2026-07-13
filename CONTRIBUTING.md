# Как внести вклад

Спасибо за интерес к проекту! Баг-репорты, идеи и pull request'ы приветствуются.

Участвуя в проекте, вы соглашаетесь соблюдать
[Кодекс поведения](CODE_OF_CONDUCT.md).

## Локальная разработка

Нужен [uv](https://docs.astral.sh/uv/) и Python 3.13 (uv поставит его сам).

```bash
git clone https://github.com/happykust/sentry-jaga
cd sentry-jaga
uv sync                  # окружение + dev-зависимости
uv run pre-commit install
```

`pre-commit` прогоняет ruff и ruff-format на каждый коммит.

## Тесты

Тесты ядра не требуют Sentry — их достаточно для большинства изменений:

```bash
uv run pytest tests/unit
```

Тесты слоя Sentry требуют самого Sentry. Его нет в PyPI (пакет `sentry` там
заморожен на 23.7.1), поэтому он ставится из исходников отдельной группой —
это долго (~157 зависимостей), но нужно один раз:

```bash
uv sync --group sentry
uv run pytest tests/integration
```

Без этой группы тесты в `tests/integration/` автоматически скипаются, так что
`uv run pytest` зелёный и без Sentry.

Ядро (клиент Яги, маппинг полей) не должно импортировать `sentry` — это
проверяется тестом изоляции. Всё, что знает про Sentry, живёт в слое интеграции.

## Стиль

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

- ruff — линт и форматирование, длина строки 100.
- mypy в режиме `strict` — новый код должен быть типизирован полностью.
- Сообщения коммитов — на английском, одной строкой, без тела
  (например: `fix: refresh token before expiry`).

Пользовательские тексты (README, описания полей в UI, сообщения об ошибках)
пишем по-русски.

## Pull request

1. Форкните репозиторий и заведите ветку от `main`.
2. Добавьте тесты на изменение поведения.
3. Убедитесь, что `ruff`, `mypy` и `pytest` зелёные.
4. Обновите `CHANGELOG.md` — секция `[Unreleased]`.
5. Откройте PR и заполните чеклист из шаблона. Опишите, что меняется и зачем;
   если правите баг — приложите шаги воспроизведения.

CI прогонит линт, типы и тесты на каждый PR.

## Проверка типов против API Sentry

`uv run mypy` по умолчанию не видит `sentry` (пакета нет в PyPI), поэтому все его
типы деградируют в `Any` — опечатка в имени метода Sentry так не поймается.

Чтобы проверить слой интеграции против **настоящего** API Sentry 26.3.1, дайте mypy
исходники Sentry (устанавливать его не нужно — только код):

```bash
git clone --depth 1 --branch 26.3.1 https://github.com/getsentry/sentry.git .sentry-src
MYPYPATH=.sentry-src/src uv run mypy --follow-imports=silent
```

Ровно это делает блокирующая CI-джоба «Типы против API Sentry». Она — основная
гарантия корректности модулей `integration.py`, `issues.py`, `sync.py`, `pipeline.py`,
`metadata.py`: их нельзя покрыть тестами, потому что тестовый стек Sentry
(Postgres/Redis/Kafka/Snuba) в CI плагина недостижим.

## Релиз

См. [docs/release.md](docs/release.md).
