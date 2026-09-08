# backend-adapter — [CC] ↔ [OI] Backend Proxy

> **v0.9.0** — HTTP-прокси-адаптер, позволяющий использовать **[CC]** (CLI)
> с бэкендом LLM, который реализует **[OI]-совместимый API** (`/v1/chat/completions`),
> но некорректно реализует протокол Anthropic Messages API.

```
[CC]  ←--Anthropic API-->  adapter (localhost:9999)  ←--[OI] API-->  LLM Backend
```

## Проблемы, которые решает адаптер

1. **System messages**. Бэкенд кластеризует system messages в конец диалога — адаптер собирает их в одно сообщение в начале.
2. **Format mismatch**. [CC] отправляет запросы в формате Anthropic Messages API, а бэкенд ожидает [OI] Chat Completions. Адаптер выполняет двунаправленную конвертацию (сообщения, инструменты, tool choice).
3. **Model compatibility**. Позволяет использовать модели Qwen (например `qwen3.6-35b-a3b`) через [CC].
4. **Qwen tool_calls fallback**. Модели Qwen иногда возвращают вызовы инструментов в текстовом формате с JSON внутри XML-подобных тегов — адаптер автоматически парсит этот формат.

## Возможности

- **Прозрачное проксирование**: ретраи/таймауты, конвертация Anthropic ↔ [OI] (в т.ч. стриминг SSE), маскирование секретов в логах.
- **Логирование и наблюдаемость**: консольные debug-блоки `[...]` печатаются **всегда** (с обрезкой до `ADAPTER_DEBUG_TRIM`; `0` — без обрезки); файловая запись per-session логов (`session-*.log`/`*.jsonl`, `*.parts` дампы) — по `ADAPTER_DEBUG_ENABLE=1` в директорию `ADAPTER_DEBUG_LOGPATH` (дефолт `./tmp/logs`), файлы несут **полные** строки без обрезки; per-session JSON/YAML-дампы всех логгируемых частей — `ADAPTER_DEBUG_PARTS=1`; **`.err`-файлы инцидентов** (v0.9.0) — при финальном ответе клиенту 4xx/5xx реального прокси-запроса пишутся в ту же директорию **безусловно** (полные запрос и ошибка, без обрезки по TRIM, вне `ADAPTER_DEBUG_ENABLE`; redact по умолчанию).
- **WEBUI** (поднимается всегда, флага отключения нет):
  - `/` — статус-страница: шапка «Backend-Adapter Version <x.x.x>» с иконками-навигацией 🔃 (перепроверить бэкенды, POST `/`) / 📋 (`/session`) / 🔧 (`/config`), LLM-эндпоинты, таблица «Models in use» с live-счётчиками вызовов/токенов и колонкой **Cost** (по тарифам `ADAPTER_MODELS_TARIFFS`; ставится live-поллингом через innerHTML — `cost_html` с сервера); действия строки — иконки-кнопки ⟳ (перепроверить эндпоинты) / ↺ (сбросить счётчики) / ✕ (удалить строку); проверка бэкендов по кнопке-иконке 🔃 (перечитывает `ADAPTER_BACKEND_CONFIG` без рестарта);
  - `/session` — просмотр сессий (`*.parts`, дерево артефактов, hash8-алиасы; обратная ссылка «Статус 📊»);
  - `/config` — переключение объёма debug-записи на лету (runtime-пул; обратная ссылка «Статус 📊»);
  - `/healthz`, `/health`, `/live`, `/ready` — health-check для оркестрации;
  - Prometheus-экспортёр (`ADAPTER_EXPORTER_ENABLE=1`, отдельный слушатель, дефолт `127.0.0.1:9100`).
- **Мониторинг использования**: таблица использованных моделей персистентна (`model-usage.yaml` в корне WEBUI = `ADAPTER_DEBUG_LOGPATH`), счётчики строки обнуляются (↺) и строка удаляется из таблицы и файла (✕) по кнопкам/API (`POST /api/model-usage/reset`, `/delete`).

## Quick start

```bash
# 1. Клонировать репозиторий
git clone https://github.com/alekseybb197/backend-adapter.git
cd backend-adapter

# 2. Установить зависимости (единственная внешняя — PyYAML)
pip install -r requirements.txt

# 3. Скопировать и заполнить env-файл и YAML-конфиг бэкенда
cp docs/samples/sample.adapter.env  adapter.env
cp docs/samples/sample.adapter.yaml adapter.yaml
#    (в adapter.env: токен ADAPTER_BACKEND_KEY_LLM_SERVICE и пути)

# 4. Загрузить env-переменные
source adapter.env

# 5. Запустить адаптер
python3 backend-adapter.py

# 6. В другом терминале запустить [CC]
claude
```

Подробная инструкция: [docs/install.md](docs/install.md)

## Документация

| Файл | Описание |
|---|---|
| [docs/install.md](docs/install.md) | Установка, конфигурация, systemd/launchd, troubleshooting |
| [docs/environment.md](docs/environment.md) | Полный справочник всех env-переменных |
| [docs/logging.md](docs/logging.md) | Конфигурация логирования и trace |
| [docs/sanitizing.md](docs/sanitizing.md) | Санитизация и маскирование секретов |
| [docs/architecture.md](docs/architecture.md) | Архитектура, диаграмма компонентов, lifecycle запросов |
| [docs/webui.md](docs/webui.md) | WEBUI и API: страницы `/`, `/session`, `/config`, `/api/*`, секция «Models in use» (live-счётчики) |

## Структура проекта

```
backend-adapter/
├── backend-adapter.py          # Точка входа (__version__)
├── backend_adapter/            # Доменный пакет (27 модулей, включая __init__.py)
│   ├── config.py               # Парсинг env, multi-backend YAML, фоновые проверки
│   ├── server.py               # HTTP-сервер (Anthropic ↔ [OI])
│   ├── convert.py              # Конвертация сообщений/инструментов
│   ├── streaming.py            # SSE streaming passthrough
│   ├── tracer.py               # JSONL trace-логирование
│   ├── session_log.py          # Per-session логи с FIFO eviction
│   ├── daemon.py               # Detach (double fork)
│   ├── logger.py               # Консольные debug-логи (безусловны)
│   ├── redact.py               # Маскирование секретов
│   ├── webserver.py            # WEBUI-ядро (роутинг эндпойнтов, serve(), CLI)
│   ├── webui_status.py         # WEBUI "/": статус-страница, usage-таблица, /api/*
│   ├── webui_ops.py            # WEBUI health: /healthz /health /live /ready
│   ├── webui_config_api.py     # WEBUI "/config": runtime-конфиг
│   ├── prometheus_exporter.py  # Метрики /metrics (отдельный слушатель)
│   ├── session_viewer.py       # WEBUI "/session": просмотр *.parts сессий
│   ├── model_usage.py          # Персистентный учёт использованных моделей, тарифы
│   ├── probe_json.py           # JSON-результаты проверок бэкендов в LOGPATH
│   ├── artifact_tree*.py       # Генерация дерева артефактов (8 модулей)
│   └── __init__.py             # Lazy-прокси глобалов config/logger/tracer на старте
├── docs/                       # Документация
│   ├── samples/                # Примеры конфигов: sample.adapter.env (env-файл),
│   │                           #   sample.adapter.yaml (YAML бэкендов),
│   │                           #   backend-adapter.service + com.user.backend-adapter.plist
│   │                           #   (шаблоны systemd/launchd для запуска из исходников)
│   └── claude_code/            # Локальные настройки клиента [CC] (settings/statusline)
└── requirements.txt            # Зависимости (единственная — PyYAML)
```

---

[GitHub](https://github.com/alekseybb197/backend-adapter) · [Changelog](changelog.md)
