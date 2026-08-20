# backend-adapter.py — Usage Guide

## What it does

`backend-adapter.py` — HTTP-прокси-адаптер, позволяющий использовать **Claude Code** (CLI) с
бэкендом LLM, который реализует **OpenAI-совместимый API** (`/v1/chat/completions`),
но некорректно реализует протокол Anthropic Messages API.

### Проблемы, которые решает адаптер

1. **System messages**. Бэкенд некорректно обрабатывает system messages (кластеризует их в конец
   диалога вместо начала). Адаптер собирает все system messages в единое сообщение в начале,
   как требует спецификация OpenAI.

2. **Format mismatch**. Claude Code отправляет запросы в формате Anthropic Messages API,
   а бэкенд ожидает формат OpenAI Chat Completions. Адаптер выполняет двунаправленную
   конвертацию:
   - Anthropic `messages` -> OpenAI `messages`
   - Anthropic `tool_use` / `tool_result` -> OpenAI `tool_calls` / `tool`
   - OpenAI response -> Anthropic response

3. **Model compatibility**. Позволяет использовать модели **Qwen** (qwen3.6-35b-a3b и аналоги)
   через Claude Code, обходя некорректную обработку Anthropic-формата на бэкенде.

4. **Qwen tool_calls fallback**. Модели Qwen иногда возвращают вызовы инструментов в
   текстовом формате с JSON-блоком, заключённым в XML-подобные теги. Адаптер автоматически
   распознаёт и парсит этот формат.

## Quick start

```bash
# 1. Загрузите переменные окружения
source adapter.env

# 2. Запустите адаптер
python3 backend-adapter.py

# 3. В другом терминале запустите Claude Code
# (Claude Code автоматически подхватит ANTHROPIC_BASE_URL)
claude
```

Или как фоновый сервис:

```bash
ADAPTER_DETACH_ENABLE=1 python3 backend-adapter.py
```

Проверка запуска:

```bash
cat /tmp/adapter.pid   # PID процесса
kill $(cat /tmp/adapter.pid)   # остановить
```

## systemd service (Linux)

Для постоянной работы адаптера как сервиса используйте systemd юнит.
Файлы `backend-adapter.service` и `backend-adapter.env` уже подготовлены.

### Установка

```bash
# 1. Скопировать юнит в директорию user-юнитов
cp backend-adapter.service ~/.config/systemd/user/backend-adapter.service

# 2. Переопределить ключ в .env на реальный
export ADAPTER_BACKEND_KEY="your-real-key-here"

# 3. Загрузить изменения
systemctl --user daemon-reload

# 4. Включить автозапуск
systemctl --user enable backend-adapter.service

# 5. Запустить
systemctl --user start backend-adapter.service

# 6. Проверить статус
systemctl --user status backend-adapter.service
```

### Управление

```bash
# Статус
systemctl --user status backend-adapter.service

# Остановить
systemctl --user stop backend-adapter.service

# Перезапустить
systemctl --user restart backend-adapter.service

# Включить/отключить автозагрузку
systemctl --user enable  backend-adapter.service
systemctl --user disable  backend-adapter.service

# Логи в реальном времени
journalctl --user -u backend-adapter -f

# Логи за последний час
journalctl --user -u backend-adapter --since "1 hour ago"
```

### Почему не detach, а systemd

| detach (ADAPTER_DETACH_ENABLE=1) | systemd |
|---|---|
| Процесс живёт сам по себе | systemd следит за процессом |
| Нет рестарта при падении | `Restart=on-failure` |
| Нет логов | логи в `journalctl` |
| PID-файл вручную | systemd сам знает PID |
| Подходит для тестов | **подходит для продакшена** |

Для продакшена используйте systemd. `ADAPTER_DETACH_ENABLE` должен быть `0`.

## macOS — launchd

На macOS вместо systemd используется **launchd**. Файл `com.user.backend-adapter.plist` уже подготовлен.

### Установка

```bash
# 1. Скопировать plist в директорию user-задач
cp com.user.backend-adapter.plist \
   ~/Library/LaunchAgents/com.user.backend-adapter.plist

# 2. Загрузить задачу
launchctl load ~/Library/LaunchAgents/com.user.backend-adapter.plist

# 3. Проверить, что работает
launchctl list | grep backend-adapter
```

### Управление

```bash
# Загрузить (запустить)
launchctl load ~/Library/LaunchAgents/com.user.backend-adapter.plist

# Выгрузить (остановить)
launchctl unload ~/Library/LaunchAgents/com.user.backend-adapter.plist

# Перезагрузить (restart = unload + load)
launchctl unload ~/Library/LaunchAgents/com.user.backend-adapter.plist
launchctl load  ~/Library/LaunchAgents/com.user.backend-adapter.plist

# Проверить статус (ищет по Label)
launchctl list | grep com.user.backend-adapter

# Проверить PID процесса
pgrep -f backend-adapter.py

# Посмотреть логи
tail -f ~/tmp/adapter.log
```

### Почему не detach, а launchd

| detach (ADAPTER_DETACH_ENABLE=1) | launchd |
|---|---|
| Процесс живёт сам по себе | launchd следит за процессом |
| Нет рестарта при падении | `KeepAlive` = авто-рестарт |
| Нет логов | логи в `StandardOutPath` |
| PID-файл вручную | launchd сам знает PID |
| Подходит для тестов | **подходит для продакшена** |

Для продакшена используйте launchd. `ADAPTER_DETACH_ENABLE` должен быть `0`.

## Environment variables — full reference

Все настройки задаются через переменные окружения.

### Подключение к бэкенду

| Variable | Default | Required | Description |
|---|---|---|---|
| ADAPTER_BACKEND_BASE | https://llm.service.example.com | yes | Base URL бэкенда |
| ADAPTER_BACKEND_KEY | (empty) | yes | API key / Bearer token для бэкенда |
| ADAPTER_PROXY_PORT | 9999 | no | Порт, на котором слушает адаптер |

### Отладка

| Variable | Default | Required | Description |
|---|---|---|---|
| ADAPTER_DEBUG_ENABLE | 1 | no | Включить логирование (1/true=yes = вкл, 0/false/no = выкл) |
| ADAPTER_DEBUG_LOGFILE | (empty) | no | Путь к файлу логов (если задан, логи идут в файл вместо консоли) |

### Фоновый режим (detach)

| Variable | Default | Required | Description |
|---|---|---|---|
| ADAPTER_DETACH_ENABLE | 0 | no | Запуск в фоне (1/true=yes = daemon, 0/false/no = foreground) |
| ADAPTER_PIDFILE | /tmp/adapter.pid | no | Путь к файлу PID в режиме daemon |

## Sample adapter.env

```bash
# ============ Adapter configuration ============

# Backend connection
export ADAPTER_BACKEND_BASE="https://llm.service.example.com"
export ADAPTER_BACKEND_KEY="your-api-key-here"

# Adapter server settings
export ADAPTER_PROXY_PORT=9999

# Debugging
# Set to 0 to suppress all console logs
export ADAPTER_DEBUG_ENABLE=1

# Or log to a file instead of console:
# export ADAPTER_DEBUG_LOGFILE="/tmp/adapter.log"

# Background service mode
# Set to 1 to detach from console and run as daemon
# export ADAPTER_DETACH_ENABLE=1

# ============ Claude Code configuration ============

# Point Claude Code to the local adapter
export ANTHROPIC_BASE_URL="http://localhost:9999"

# Empty key — the adapter handles auth to the backend itself
export ANTHROPIC_API_KEY=""

# Disable telemetry / attribution
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"
export CLAUDE_CODE_ATTRIBUTION_HEADER="0"

# Use Qwen model through the adapter
export ANTHROPIC_MODEL="qwen3.6-35b-a3b"
export ANTHROPIC_DEFAULT_MODEL="qwen3.6-35b-a3b"

# Disable thinking mode (not supported by all backends)
export CLAUDE_CODE_DISABLE_THINKING="1"
```

## How it works

```
Claude Code  <--Anthropic API-->  adapter  <--OpenAI API-->  LLM Backend
     |                                     |
     | localhost:9999                      | converts formats
```

1. Claude Code sends Anthropic-format POST to `localhost:9999/v1/messages`
2. Adapter converts to OpenAI format
3. Adapter forwards to backend (`/v1/chat/completions`)
4. Adapter converts OpenAI response back to Anthropic format
5. Adapter returns response to Claude Code

## Detach mode details

When `ADAPTER_DETACH_ENABLE=1`:
- The process forks twice (UNIX daemon pattern)
- Parent exits immediately
- stdio/stderr redirected to /dev/null
- PID file written (default: `/tmp/adapter.pid`)
- Logs go to `ADAPTER_DEBUG_LOGFILE` if set

## Troubleshooting

**Port already in use**:
```bash
lsof -i :9999
ADAPTER_PROXY_PORT=9998
```

**No response from backend**:
```bash
ADAPTER_DEBUG_ENABLE=1
# or
ADAPTER_DEBUG_LOGFILE=/tmp/adapter.log
```

**Qwen tool calls garbled**: Expected — adapter has a fallback parser for this.
