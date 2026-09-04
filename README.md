# backend-adapter — Claude Code ↔ OpenAI Backend Proxy

> **v0.7.2** — HTTP-прокси-адаптер, позволяющий использовать **Claude Code** (CLI)
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

# 3. Загрузить env-переменные
source sample.adapter.env

# 4. Запустить адаптер
python3 backend-adapter.py

# 5. В другом терминале запустить Claude Code
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

## Структура проекта

```
backend-adapter/
├── backend-adapter.py          # Точка входа
├── backend_adapter/            # Доменный пакет (11 модулей)
│   ├── config.py               # Парсинг env, multi-backend, YAML
│   ├── server.py               # HTTP-сервер
│   ├── convert.py              # Anthropic ↔ OpenAI конвертация
│   ├── streaming.py            # SSE streaming passthrough
│   ├── tracer.py               # JSONL trace-логирование
│   ├── session_log.py          # Per-session логи с FIFO eviction
│   ├── skill.py                # Детекция скиллов
│   ├── daemon.py               # Detach (double fork)
│   ├── logger.py               # Debug-логирование
│   ├── redact.py               # Маскирование секретов
│   └── __init__.py             # Module-level proxy
├── docs/                       # Документация
├── backend-adapter.service     # systemd unit (Linux)
├── com.user.backend-adapter.plist  # launchd (macOS)
├── backend-adapter.env         # Пример env для сервиса
├── sample.adapter.env          # Полный пример env
├── sample.adapter.yaml         # Пример YAML-конфига бэкендов
└── requirements.txt            # Зависимости (единственная — PyYAML)
```

---

[GitHub](https://github.com/alekseybb197/backend-adapter) · [Changelog](changelog.md)
