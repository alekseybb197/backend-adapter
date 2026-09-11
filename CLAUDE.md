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
  `changelog.md`. Пакет `backend_adapter/` (layout и назначение каждого модуля —
  `docs/architecture.md` §2; группа `artifact_tree*.py`, публичный API —
  `artifact_tree.generate()`).
- **Python 3.10+** (аннотации `X | Y`); единственная внешняя зависимость —
  **PyYAML** (`requirements.txt`), всё остальное — стандартная библиотека.
- Документация: `docs/install.md` (запуск/шаблоны продакшена),
  `docs/environment.md` (все env-переменные), `docs/logging.md` (каналы логов),
  `docs/sanitizing.md`, `docs/architecture.md` (layout §2, наблюдаемость §8,
  DAG §10), `docs/webui.md` (WEBUI/эндпоинты).
- Рабочие копии конфигов кладутся в корень репозитория как
  `adapter.env`/`adapter.yaml` (в `.gitignore`); образцы — `docs/samples/`.
- Каналы логов (детали — `docs/logging.md`): консольные debug-логи безусловны
  и обрезаются до `ADAPTER_DEBUG_TRIM`; файловая запись гейтится
  `ADAPTER_DEBUG_ENABLE` (`session-*.log` — полные строки, `*.jsonl` — trace,
  `*.parts` — дампы). Вне ENABLE/PARTS/TRIM в `ADAPTER_DEBUG_LOGPATH` живут
  безусловные артефакты: `.err`-файлы инцидентов и WARN-событий (v0.9.1),
  JSON-результаты проверок бэкендов (`probe_json.py`), PID-файл (`daemon.py`).

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
(`config._resolve_backend`), решает **TARGET-маршрутизацию** (`routing.decide`) и
ретранслирует в бэкенд; конверсия форматов — `convert.py` (не-стрим) /
`streaming.py` (стрим: конверсия SSE → Anthropic или passthrough-релей
`relay_sse`). Наблюдаемость — `tracer.py` (JSONL trace), `logger.py` (консоль),
`session_log.py` (per-session файлы + `.err`), `redact.py` (секреты — везде).
Детали потока — `docs/architecture.md` §4–§6.

**Инвариант DAG:** база без внутренних зависимостей при импорте — `redact.py`,
`session_log.py`, `daemon.py`, `config.py`, `artifact_tree_common.py`,
`webserver.py` (эндпоинты импортирует только внутри `serve()`); все остальные
модули зависят минимум от одного из них. Новые модули — без циклов
(dependency graph — `docs/architecture.md` §10).

**WEBUI** (поднимается всегда — флага отключения нет): ядро `webserver.py`
(реестр эндпоинтов, `serve()`) в daemon-потоке + модули-эндпоинты
(`webui_status.py` `/`, `webui_ops.py` health, `webui_config_api.py` `/config`,
`session_viewer.py` `/session`); корень — `ADAPTER_DEBUG_LOGPATH`. Prometheus-
экспортёр (`ADAPTER_EXPORTER_ENABLE=1`) — отдельный слушатель, не эндпоинт WEBUI.
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
