# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
  (27 модулей → 28 с routing.py — входные эндпоинты/TARGET, включая
  `__init__.py`; генератор дерева
  артефактов —
  группа модулей `artifact_tree*.py` из 8 файлов, публичный API —
  `artifact_tree.generate()`; см. `docs/architecture.md`).
- **Python 3.10+** (аннотации `X | Y`).
- Единственная внешняя зависимость — **PyYAML** (`requirements.txt`);
  всё остальное — стандартная библиотека.
- Полная документация: `docs/install.md`, `docs/environment.md`,
  `docs/logging.md`, `docs/sanitizing.md`, `docs/architecture.md`,
  `docs/webui.md`.
- Версия объявляется в `backend-adapter.py` (`__version__`), история — в `changelog.md`.
- **WEBUI** (поднимается всегда — флага отключения нет, v0.8.6): общее ядро `webserver.py`
  (роутинг эндпойнтов, `serve()`, CLI `python -m backend_adapter.webserver`) +
  эндпойнт-модули: `/` =
  `webui_status.py` (версия, LLM-эндпойнты, модели; секция «Models in use» —
  таблица `model_usage.py` персистентна: YAML `model-usage.yaml` в корне WEBUI
  (= `ADAPTER_DEBUG_LOGPATH`),
  обнуление счётчиков строки — кнопка ⏪/POST `/api/model-usage/reset` (строка
  не удаляется), удаление строки из таблицы и файла — кнопка 🗑/POST
  `/api/model-usage/delete` (в отличие от reset строка уходит и из памяти,
  и из YAML — файл перезаписывается сразу без неё), перепроверка
  эндпоинтов строки — кнопка 🔄/POST `/api/model-usage/reprobe`; действия
  строки — иконки-кнопки 🔄/⏪/🗑 в колонке Actions (фразы-подписи в
  title/aria-label); счётчики Вызовов/Input/Output
  на открытой странице обновляются сами — JS usage_poll ~5 с → GET
  `/api/model-usage/snapshot`, без сети к бэкендам (Cost-ячейка ставится
  тем же поллингом через innerHTML — `setHtml`/`cost_html` с сервера,
  v0.9.0); шапка статус-страницы — «Backend-Adapter Version <x.x.x>»,
  второй строкой иконки-навигация: 🔃 (кнопка POST `/`, проверка бэкендов,
  подпись «Перепроверить бэкенды» в title/aria-label, PRG/303),
  📋 → `/session`, 🔧 → `/config`; обратные ссылки «Статус 📊» на `/session`
  и `/config`; проверка бэкендов —
  фоновая: при старте адаптера, на первом GET `/` и по кнопке 🔃
  (POST `/`) → `config.start_refresh()` (кнопка
  перечитывает `ADAPTER_BACKEND_CONFIG` — бэкенды добавляются/удаляются
  без рестарта; битый YAML — прежние остаются `[WARN]`); POST отвечает
  303 See Other на GET `/` (PRG — нет диалога «повторить действие»);
  пока проверка идёт, страница показывает баннер
  «Проверка выполняется…» и авто-обновляется по завершении через
  `/api/refresh-state`; футер — «Список провайдеров обновлён в … (N провайдеров, M моделей).»),
  `/healthz` `/health` `/live` `/ready` = `webui_ops.py` (health-check на том же
  слушателе: JSON с версией/uptime/pid; `/ready` — 200 когда бэкенды настроены
  и кэш моделей непуст, иначе 503), `/session` = `session_viewer.py` (вкладки + раздача файлов + hash8-алиасы
  `/session/<hash8>/...` и png/puml-шорткаты; корень — та же директория
  `ADAPTER_DEBUG_LOGPATH`, порт `ADAPTER_WEBUI_PORT`, адрес `ADAPTER_WEBUI_HOST`,
  daemon-поток в процессе адаптера), `/config` = `webui_config_api.py`
  (runtime-пул из 12 переменных — объём debug-записи, санитайзер, рубильники
  стриминга/usage и строгой валидации моделей; см. `RUNTIME_CONFIG_POOL`
  в `config.py`; сеть/бэкенды/порты/`ADAPTER_DEBUG_LOGPATH` на лету не меняются).
  Prometheus-экспортёр (`ADAPTER_EXPORTER_ENABLE=1`) — ОТДЕЛЬНЫЙ лёгкий
  слушатель на `ADAPTER_EXPORTER_PORT` (дефолт 9100) и адресе
  `ADAPTER_WEBUI_HOST`, НЕ эндпоинт WEBUI (модуль `prometheus_exporter.py`,
  stdlib-only, text exposition 0.0.4): настройки/статус приложения,
  по бэкенду up/models/endpoint, по модели счётчики calls/input/output.
  Адрес прослушивания самого адаптера — `ADAPTER_ENDPOINT_HOST`
  (обе по умолчанию `127.0.0.1`);
  `artifact_tree.py` — генерация дерева артефактов (`artefacts/tree.html`)
  на лету; повторные генерации инкрементальны: чекпоинт
  `artefacts/.build_state.json` ограничивает пересборку новыми `*.parts`
  файлами сессии; большие сессии разбиваются на страницы пагинации
  `artefacts/pages/<N>/`.
  Новый эндпойнт = модуль с `@webserver.register` + импорт в `webserver.serve()`.
  Консольные debug-логи безусловны (печатаются всегда — v0.8.6) и обрезаются
  до `ADAPTER_DEBUG_TRIM` (0 — без обрезки); `ADAPTER_DEBUG_ENABLE` гейтит
  только файловую запись (дефолт `0`): `session-*.log` несёт ПОЛНЫЕ строки
  без обрезки, `*.jsonl` — trace. Per-session дампы частей протокола
  (парные `.json`+`.yaml`, ВСЕ логгируемые части — фиксированного списка
  тегов больше нет) включаются флагом `ADAPTER_DEBUG_PARTS=1`;
  файлы пишутся только при `ADAPTER_DEBUG_ENABLE=1` в директорию
  `ADAPTER_DEBUG_LOGPATH`. Помимо гейтнутых каналов в той же директории
  живут БЕЗУСЛОВНЫЕ артефакты (вне ENABLE/PARTS/TRIM; см. `probe_json.py`,
  `session_log.write_error_file`): JSON-результаты проверок
  `<имя_бэкенда>.models.json` и `<имя_бэкенда>.<модель>.<pname>.json`
  (перезапись каждый раз, секреты маскируются; v0.9.0), файлы `.err`
  инцидентов и PID-файл `adapter.pid` при запуске в фоне (`daemon.py`).
- Примеры конфигов — в `docs/samples/`: `sample.adapter.env` (env-файл
  адаптера), `sample.adapter.yaml` (конфиг бэкендов), шаблоны продакшена
  `backend-adapter.service` (systemd, запуск из исходников) и
  `com.user.backend-adapter.plist` (launchd); рабочие копии кладутся в
  корень репозитория как `adapter.env`/`adapter.yaml` (в `.gitignore`).

## Принципы

- Не ломать «нулевую настройку»: минимальный запуск — `ADAPTER_BACKEND_CONFIG`
  (путь к YAML-файлу `backend:`) + env-переменная токена, на которую ссылается
  поле `key`.
- Все секреты в логах маскируются (`redact.py`); санитайзер включён по умолчанию.
- Новые возможности покрываются флагами окружения (см. `docs/environment.md`),
  дефолты выбирают безопасное поведение.
- Документация обновляется вместе с кодом, а не после.

## Архитектура (большая картина)

Запрос `[CC]` → адаптер: `server.py` читает тело, резолвит `model`
(strict/маппинг), выбирает бэкенд (`config._resolve_backend`), решает
**TARGET-маршрутизацию** (`routing.decide`) и ретранслирует в бэкенд;
конверсия форматов — `convert.py` (не-стрим) / `streaming.py`
(стрим: конверсия SSE → Anthropic **или** passthrough-релей `relay_sse`).
Наблюдаемость — `tracer.py` (JSONL trace), `logger.py` (консольные
debug-блоки, безусловны), `session_log.py` (per-session файлы + `.err`),
`redact.py` (маскирование секретов — везде).

**Инвариант DAG:** `redact.py`, `session_log.py`, `daemon.py`, `config.py`,
`artifact_tree_common.py`, `webserver.py` (эндпоинты импортирует только
внутри `serve()`) — без внутренних зависимостей при импорте (база DAG);
все остальные модули зависят как минимум от одного из них. Новые модули
ставятся в DAG так, чтобы не создавать циклы (см. dependency graph в
`docs/architecture.md` §10 — источник правды).

**WEBUI:** ядро `webserver.py` (реестр эндпоинтов, `serve()`, CLI
`python -m backend_adapter.webserver`) поднимается в daemon-потоке;
каждый эндпоинт — отдельный модуль-эндпоинт (`webui_status.py` `/`,
`webui_ops.py` health, `webui_config_api.py` `/config`,
`session_viewer.py` `/session`). Новый эндпоинт = модуль с
`@webserver.register` + импорт в `webserver.serve()`.

**Безусловные каналы** (вне `ADAPTER_DEBUG_ENABLE`/`PARTS`/`TRIM`):
консольные debug-логи, `.err`-файлы инцидентов, JSON-результаты проверок
(`probe_json.py`), PID-файл — все в `ADAPTER_DEBUG_LOGPATH`.

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
