# CLAUDE.md — backend-adapter

> Локальный проектный контекст для Claude Code. Входит в состав репозитория —
> обновляется вместе с кодом (см. changelog.md).

## Цели проекта

**backend-adapter** — HTTP-прокси, позволяющий работать **Claude Code** (CLI) с
бэкендом LLM, который реализует **OpenAI-совместимый API**
(`/v1/chat/completions`), но некорректно обрабатывает протокол
Anthropic Messages API.

```
Claude Code  <--Anthropic API-->  adapter (localhost:9999)  <--OpenAI API-->  LLM Backend
```

Адаптер решает четыре проблемы:
1. **System messages** — бэкенд кластеризует их в конец диалога; адаптер
   собирает их в одно сообщение в начале, как требует спецификация OpenAI.
2. **Format mismatch** — двунаправленная конвертация сообщений, инструментов
   и tool choice между Anthropic Messages API и OpenAI Chat Completions.
3. **Model compatibility** — использование моделей Qwen через Claude Code.
4. **Qwen tool_calls fallback** — парсинг вызовов инструментов, которые
   модели Qwen иногда возвращают текстом в XML-подобных тегах.

Приоритеты: **надёжность и наблюдаемость** (retry/timeout/trace-логирование),
**минимум зависимостей**, **простая конфигурация через переменные окружения**.

## Ключевые факты

- Точка входа: `backend-adapter.py`; доменный пакет `backend_adapter/`
  (22 модуля, включая `__init__.py`; генератор дерева артефактов —
  пакет `artifact_tree*` из 8 модулей, публичный API — `artifact_tree.generate()`;
  см. `docs/architecture.md`).
- **Python 3.10+** (аннотации `X | Y`).
- Единственная внешняя зависимость — **PyYAML** (`requirements.txt`);
  всё остальное — стандартная библиотека.
- Полная документация: `docs/install.md`, `docs/environment.md`,
  `docs/logging.md`, `docs/sanitizing.md`, `docs/architecture.md`.
- Версия объявляется в `backend-adapter.py` (`__version__`), история — в `changelog.md`.
- **WEBUI** (`ADAPTER_WEBUI_ENABLE=1`): общее ядро `webserver.py` (роутинг эндпойнтов,
  `serve()`, CLI `python -m backend_adapter.webserver`) + эндпойнт-модули: `/` =
  `webui_status.py` (версия, LLM-эндпойнты, модели; обновление списка
  моделей при загрузке страницы и по кнопке — `config.refresh_models()`),
  `/session` = `session_viewer.py` (вкладки + раздача файлов; корень — директория
  `ADAPTER_DEBUG_LOGFILE`, порт `ADAPTER_WEBUI_PORT`, daemon-поток в процессе адаптера);
  `artifact_tree.py` — генерация дерева артефактов (`artefacts/tree.html`) на лету.
  Новый эндпойнт = модуль с `@webserver.register` + импорт в `webserver.serve()`.
  Per-session дампы частей протокола включаются флагом `ADAPTER_DEBUG_TAGS_OUT=1`
  (парные `.json`+`.yaml`, фиксированный список тегов), требуют директорию
  в `ADAPTER_DEBUG_LOGFILE`.
- Служебные файлы продакшена: `backend-adapter.service` (systemd),
  `com.user.backend-adapter.plist` (launchd).

## Принципы

- Не ломать «нулевую настройку»: минимальный запуск — два параметра
  (`ADAPTER_BACKEND_BASE`, `ADAPTER_BACKEND_KEY`).
- Все секреты в логах маскируются (`redact.py`); санитайзер включён по умолчанию.
- Новые возможности покрываются флагами окружения (см. `docs/environment.md`),
  дефолты выбирают безопасное поведение.
- Документация обновляется вместе с кодом, а не после.

## Проверка перед коммитом — обязательна

Перед каждым коммитом гонять в `venv/` все четыре команды (в этом порядке);
зелёные все четыре — только тогда коммит:

```bash
# Linting and formatting
ruff check backend_adapter/ backend-adapter.py
ruff format --check backend_adapter/ backend-adapter.py
# Type checking
mypy --strict --ignore-missing-imports backend_adapter/ backend-adapter.py
# Tests
pytest
```

Контекст: lint/typecheck идут по `backend_adapter/ backend-adapter.py`
(тесты не покрываются), mypy — в strict-режиме, несмотря на не-strict
конфиг `pyproject.toml` (disable_error_code там всё ещё действует;
strict-ошибки в пакете починены и регрессий быть не должно).

## Журнал решений (ADR)

Полная история изменений — в changelog.md. В журнал вносятся только
решения, затрагивающие архитектуру проекта или существенный функционал
(новый компонент/модуль, контракты протокола, констрейнты зависимостей,
каркас тестирования). Мелкие правки — ссылки на страницах, форматирование,
точечные багфиксы без смены контракта — в журнал не вносятся, им место
только в changelog.md. Записи накапливаются здесь, новые сверху; каждая
запись журнала сопровождается блоком в changelog.md.

### 2026-09-02 — WEBUI просмотра сессий + единый флаг дампов (v0.7.0)

**Контекст:** длинные сессии (что агент отправлял модели и что получал —
reasoning, tool calls, результаты инструментов) невозможно разбирать по
сырым `.parts`-дампам. Для просмотра существовали самодостаточные скрипты
в `tmp/` (session_viewer.py + artifact_tree.py), жившие вне пакета.

**Решение:**
- скрипты перенесены в пакет (`backend_adapter/session_viewer.py`,
  `backend_adapter/artifact_tree.py`); из `artifact_tree.py` убран встроенный
  `configure_logging()` (basicConfig перехватывал бы root-логгер адаптера);
- запуск — при `ADAPTER_WEBUI_ENABLE=1` в **daemon-потоке процесса адаптера**
  (не дочерний процесс); корень — директория `ADAPTER_DEBUG_LOGFILE`;
  условие для WEBUI и для дампов общее: `ADAPTER_DEBUG_LOGFILE` = директория;
- две теговые переменные (`ADAPTER_DEBUG_TAGS_JSON`/`_YAML`) объединены в
  один булев флаг `ADAPTER_DEBUG_TAGS_OUT`: дампы пишутся комплектом для
  фиксированного списка тегов, `.json`+`.yaml` парой.

**Следствия:** новые фичи — за флагами окружения (`ADAPTER_WEBUI_ENABLE`,
`ADAPTER_WEBUI_PORT`, `ADAPTER_DEBUG_TAGS_OUT`), дефолты безопасны (выкл).

### 2026-09-01 — Тестовый каркас: pytest, модуль → файл тестов

**Контекст:** 11 модулей / ~2300 строк без единого теста при активном
проектировании (за день — 5 версий). Дальнейшие изменения (конвертация
протоколов, стриминг, ретраи) ломались бы незаметно.

**Решение:**
- добавлен `tests/` (один файл на модуль) + `pytest.ini` +
  `requirements-dev.txt` (`pytest>=8`); запуск — `venv/bin/pytest`
  (138 тестов, без сети, без бэкендов, без ключей);
- изоляция: фикстура `fresh_env` (env + `importlib.reload` config),
  autouse-фикстура `isolate_logs` (сброс глобалов session_log/tracer/config);
- интеграция сервера — фикстура `fake_backend`: `ThreadingHTTPServer` на
  случайном порту, конфигурация ответов через property-facade над class-атрибутами
  хэндлера (instance-атрибуты не видны: хэндлер пересоздаётся на каждый запрос);
- `ADAPTER_PROXY_PORT` в тестах — **9998** (9999 занят рабочим адаптером).

**Следствия:** любой новый модуль получает `tests/test_<module>.py`;
логика, читающая `os.environ` при импорте, тестируется только через
перезагрузку модулей; тесты не пишут файлы вне своих tmp-директорий.

### 2026-08-31 — Официально зафиксированы зависимость от PyYAML и минимум Python 3.10+

**Контекст:** с появлением YAML-дампов в сессионных логах (коммит `becdabd`)
`session_log.py` безусловно импортирует `yaml`, но документация заявляла
«только стандартная библиотека», манифеста зависимостей не было, а аннотации
`X | Y` уже требовали Python ≥3.10 при заявленном 3.8+.

**Решение:**
- добавлен `requirements.txt` (`PyYAML>=6.0`), установка задокументирована
  в README и `docs/install.md`;
- официальный минимум — **Python 3.10+**;
- ленивый импорт PyYAML **не** делался — зависимость остаётся безусловной.

**Следствия:** код не меняется; при любых будущих изменениях держать
`requirements.txt` в актуальном состоянии; заявлять «только стандартная
библиотека» можно лишь для отдельных модулей (а не проекта в целом).
