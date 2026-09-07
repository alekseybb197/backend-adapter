# backend-adapter — Claude Code ↔ OpenAI Backend Proxy

> **v0.8.4** — HTTP-прокси-адаптер, позволяющий использовать **Claude Code** (CLI)
> с бэкендом LLM, который реализует **OpenAI-совместимый API** (`/v1/chat/completions`),
> но некорректно реализует протокол Anthropic Messages API.

```
Claude Code  ←--Anthropic API-->  adapter (localhost:9999)  ←--OpenAI API-->  LLM Backend
```

## Проблемы, которые решает адаптер

1. **System messages**. Бэкенд кластеризует system messages в конец диалога — адаптер собирает их в одно сообщение в начале.
2. **Format mismatch**. Claude Code отправляет запросы в формате Anthropic Messages API, а бэкенд ожидает OpenAI Chat Completions. Адаптер выполняет двунаправленную конвертацию (сообщения, инструменты, tool choice).
3. **Model compatibility**. Позволяет использовать модели Qwen (например `qwen3.6-35b-a3b`) через Claude Code.
4. **Qwen tool_calls fallback**. Модели Qwen иногда возвращают вызовы инструментов в текстовом формате с JSON внутри XML-подобных тегов — адаптер автоматически парсит этот формат.

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

# 6. В другом терминале запустить Claude Code
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
├── backend-adapter.py          # Точка входа
├── backend_adapter/            # Доменный пакет (26 модулей, включая __init__.py)
│   ├── config.py               # Парсинг env, multi-backend, YAML
│   ├── server.py               # HTTP-сервер
│   ├── convert.py              # Anthropic ↔ OpenAI конвертация
│   ├── streaming.py            # SSE streaming passthrough
│   ├── tracer.py               # JSONL trace-логирование
│   ├── session_log.py          # Per-session логи с FIFO eviction
│   ├── daemon.py               # Detach (double fork)
│   ├── logger.py               # Debug-логирование
│   ├── redact.py               # Маскирование секретов
│   ├── webserver.py            # WEBUI-ядро (роутинг эндпойнтов, serve(), CLI)
│   ├── webui_status.py         # WEBUI "/": статус-страница
│   ├── webui_ops.py            # WEBUI health: /healthz /health /live /ready
│   ├── webui_config_api.py     # WEBUI "/config": runtime-конфиг
│   ├── prometheus_exporter.py  # Метрики /metrics (отдельный слушатель)
│   ├── session_viewer.py       # WEBUI "/session": просмотр *.parts сессий
│   ├── artifact_tree*.py       # Генерация дерева артефактов (8 модулей)
│   └── __init__.py             # Module-level proxy
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
