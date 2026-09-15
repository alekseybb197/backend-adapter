# CLAUDE.md

This file provides guidance to [CC] () when working with code in this repository.

> Локальный проектный контекст для [CC]. Входит в состав репозитория —
> обновляется вместе с кодом (см. changelog.md).

## Цели проекта

**backend-adapter** — HTTP-прокси, позволяющий работать агентам с
**Anthropic-совместимым API** (**[CC]**, **QwenCode**) через бэкенд LLM,
который реализует **[OI]-совместимый API** (`/v1/chat/completions`), но
некорректно обрабатывает протокол Anthropic Messages API.

```
[CC] / QwenCode  <--Anthropic API-->  adapter (localhost:9999)  <--[OI] API-->  LLM Backend
```

Адаптер решает четыре проблемы:
1. **System messages** — бэкенд кластеризует их в конец диалога; адаптер
   собирает их в одно сообщение в начале, как требует спецификация [OI].
2. **Format mismatch** — двунаправленная конвертация сообщений, инструментов
   и tool choice между Anthropic Messages API и [OI] Chat Completions.
3. **Model compatibility** — использование моделей Qwen через [CC] и QwenCode.
4. **Qwen tool_calls fallback** — парсинг вызовов инструментов, которые
   модели Qwen иногда возвращают текстом в XML-подобных тегах.

Приоритеты: **надёжность и наблюдаемость** (retry/timeout/trace-логирование),
**минимум зависимостей**, **простая конфигурация через переменные окружения**.

## Ключевые факты

- Точка входа: `backend-adapter.py`; `__version__` — источник версии, история —
  `changelog.md`. `__comment__` — **краткое фиксированное** резюме назначения
  адаптера, при релизах не изменяется (нигде не парсится; версию читает
  `webserver._detect_version()` из `__version__`). Пакет `backend_adapter/`
  (layout и назначение каждого модуля — `docs/architecture.md` §2; группа
  `artifact_tree*.py`, публичный API — `artifact_tree.generate()`).
- **Python 3.10+** (аннотации `X | Y`); единственная внешняя зависимость —
  **PyYAML** (`requirements.txt`), всё остальное — стандартная библиотека.
- Документация: `docs/install.md` (запуск/шаблоны продакшена),
  `docs/environment.md` (все env-переменные), `docs/logging.md` (каналы логов),
  `docs/sanitizing.md`, `docs/architecture.md` (layout §2, наблюдаемость §8,
  DAG §10), `docs/webui.md` (WEBUI/эндпоинты), `docs/claude_code.md`,
  `docs/qwen-code.md`, `docs/codex.md` (гайды клиентов).
- Рабочие копии конфигов кладутся в корень репозитория как
  `adapter.env`/`adapter.yaml` (в `.gitignore`); образцы — `docs/samples/`.
- Каналы логов (детали — `docs/logging.md`): консольные debug-логи безусловны
  и обрезаются до `ADAPTER_DEBUG_TRIM`; файловая запись гейтится
  `ADAPTER_DEBUG_ENABLE` (`session-*.log` — полные строки, `*.jsonl` — trace,
  `*.parts` — дампы). Вне ENABLE/PARTS/TRIM в `ADAPTER_DEBUG_LOGPATH` живут
  безусловные артефакты: `.err`-файлы инцидентов и WARN-событий (v0.9.1),
  JSON-результаты проверок бэкендов (`probe_json.py`), PID-файл (`daemon.py` —
  пишется при ЛЮБОМ запуске, не только в detach; удаляется при штатном
  завершении и через `atexit`).

## Принципы

- Не ломать «нулевую настройку»: минимальный запуск — `ADAPTER_BACKEND_CONFIG`
  (путь к YAML-файлу `backend:`) + env-переменная токена, на которую ссылается
  поле `key`.
- Все секреты в логах маскируются (`redact.py`); санитайзер включён по умолчанию.
- Новые возможности покрываются флагами окружения (см. `docs/environment.md`),
  дефолты выбирают безопасное поведение.
- Документация обновляется вместе с кодом, а не после.

## Архитектура (большая картина)

Запрос клиента ([CC] / QwenCode) → адаптер: `server.py` резолвит `model` (strict/маппинг) и бэкенд
(`config._resolve_backend`), решает **TARGET-маршрутизацию** (`routing.decide`,
с учётом пер-сессионных переопределений `session_settings`) и
ретранслирует в бэкенд; конверсия форматов — `convert.py` (не-стрим) /
`streaming.py` (стрим: конверсия SSE → Anthropic или passthrough-релей
`relay_sse`). Наблюдаемость — `tracer.py` (JSONL trace), `logger.py` (консоль),
`session_log.py` (per-session файлы + `.err`), `redact.py` (секреты — везде).
Детали потока — `docs/architecture.md` §4–§6.

**Сессия — единица управления (v0.9.5):** `session_registry.py` ведёт таблицу
сессий (страница `/sessions`), `session_settings.py` хранит пер-сессионные
переопределения Log/Parts/TARGET (живут в памяти процесса). **Две модели
наследования (v0.9.8):** Log/Parts — **снимок** общих флагов в момент
образования сессии (`ensure_session`), дальше сессия живёт своими значениями,
состояния `inherit` нет; глобальные `ADAPTER_DEBUG`/`ADAPTER_DEBUG_PARTS` —
лишь шаблон для **новых** сессий (ничего не включают/выключают на ходу).
TARGET-поля — **живое** наследование (два состояния: не задано / значение;
v0.9.9 — «вернуться к общему» = снять переопределение, а WEBUI снимает запись
при выборе значения, равного текущему общему). Флаги логирования читаются через
`session_log.logging_enabled`/`parts_enabled` (запрос без `session_id` или с
`unknown` не пишет файлы вообще); TARGET — через `routing.decide`
при непустом `session_id`. `.err` пишется для **любой** ошибки распознанного
входного пути (`_req_ctx.err_eligible`) и **не** зависит от
`ADAPTER_SESSIONS_TABLE` (v0.9.7); счётчик «Ошибок» строки ведётся только при
живой таблице. Детали — `docs/routing.md` §2.4, `docs/webui.md` §4,
`docs/logging.md`.

**Персистентность runtime-пула (v0.9.6):** `state_store.py` хранит значения
`RUNTIME_CONFIG_POOL` в `state.yaml` (env `ADAPTER_STATE`, в `ADAPTER_DEBUG_LOGPATH`)
— на старте файл применяется **поверх env** (`apply_on_startup`, вызывается из
`backend-adapter.py` до `_init_multi_backends`), каждое изменение `/config`
персистится через колбек `config.set_on_change` (config остаётся корнем DAG).
Пер-сессионные настройки НЕ сохраняются. **Связка Log/Parts:** Parts активен
только при Log — каскад на двух уровнях: глобальный в
`config.set_runtime_config` и пер-сессионный в `session_settings.set_config`
(Parts=on поднимает Log, Log=off гасит Parts); финальный гейт —
`session_log.parts_enabled` (проверяет `logging_enabled`). **Валидация env** (`env_validate.py`) — строгая, до
импорта config: невалидный int/bool → `[FATAL]` + `sys.exit(1)`.

**Инвариант DAG:** база без внутренних зависимостей при импорте — `redact.py`,
`session_log.py`, `daemon.py`, `config.py`, `artifact_tree_common.py`,
`webserver.py` (эндпоинты импортирует только внутри `serve()`); все остальные
модули зависят минимум от одного из них. `session_settings.py`,
`session_registry.py`, `state_store.py` и `env_validate.py` — листы DAG
(`env_validate` — stdlib-only, остальные импортируют только `config`). Новые
модули — без циклов (dependency graph — `docs/architecture.md` §10).

**WEBUI** (поднимается всегда — флага отключения нет): ядро `webserver.py`
(реестр эндпоинтов, `serve()`) в daemon-потоке + модули-эндпоинты
(`webui_status.py` `/`, `webui_sessions.py` `/sessions`, `webui_ops.py` health,
`webui_config_api.py` `/config`, `session_viewer.py` `/session`); корень —
`ADAPTER_DEBUG_LOGPATH`. Prometheus-экспортёр (`ADAPTER_EXPORTER_ENABLE=1`) —
отдельный слушатель, не эндпоинт WEBUI.
Новый эндпоинт = модуль с `@webserver.register` + импорт в `webserver.serve()`.
Поведение страниц и API — `docs/webui.md`.

**Завершение по Ctrl-C/SIGTERM** (v0.9.1, `backend_adapter/shutdown.py`): первый
сигнал обрабатывает единый хэндлер `_first_signal`, который сразу переключает
SIGINT/SIGTERM на немедленный `os._exit(130)` и лишь затем делает raise
KeyboardInterrupt — повторный/задвоенный сигнал (PyInstaller bootloader; сборка
с `--bootloader-ignore-signals`) умирает тихо, без traceback. Итог: вежливый
выход (`[EXIT] Bye`, rc 0) / повторный сигнал → 130.

## Команды

Работа ведётся в виртуальном окружении `venv/` (обязательно):

```bash
source venv/bin/activate

# Запуск адаптера (минимальный запуск — ADAPTER_BACKEND_CONFIG + токен)
set -a; source adapter.env; set +a
python backend-adapter.py

# Полный прогон тестов (в т.ч. manual-интеграция, ~30 с)
pytest
# Одиночный тест / по подстроке
pytest tests/test_routing.py
pytest -k relay_sse
# Только интеграционный прогон реального процесса
pytest -m manual
```

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
