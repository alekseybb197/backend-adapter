# Установка — backend-adapter

> **backend-adapter** (v0.6.9) — HTTP-прокси-адаптер, позволяющий использовать **Claude Code** (CLI)
> с бэкендом LLM, который реализует **OpenAI-совместимый API** (`/v1/chat/completions`),
> но некорректно обрабатывает протокол Anthropic Messages API.

## Обзор

Адаптер решает четыре проблемы:

1. **System messages**. Бэкенд кластеризует system messages в конец диалога — адаптер собирает их
   в одно сообщение в начале, как требует спецификация OpenAI.
2. **Format mismatch**. Claude Code отправляет запросы в формате Anthropic Messages API,
   а бэкенд ожидает OpenAI Chat Completions. Адаптер выполняет двунаправленную конвертацию
   (сообщения, инструменты, tool choice).
3. **Model compatibility**. Позволяет использовать модели Qwen (например `qwen3.6-35b-a3b`)
   через Claude Code.
4. **Qwen tool_calls fallback**. Модели Qwen иногда возвращают вызовы инструментов в текстовом
   формате с JSON внутри XML-подобных тегов — адаптер автоматически парсит этот формат.

Схема работы:

```
Claude Code  <--Anthropic API-->  adapter (localhost:9999)  <--OpenAI API-->  LLM Backend
```

Без внешних зависимостей — только стандартная библиотека Python.

---

## 1. Требования

- **Python 3.8+** (используется только stdlib: `urllib`, `json`, `threading`, `ssl`)
- **git** (для клонирования репозитория)
- **bash/zsh** (для запуска через терминал)

---

## 2. Клонирование

```bash
# Клонировать репозиторий
git clone https://github.com/alekseybb197/backend-adapter.git
cd backend-adapter
```

Структура проекта:

```
backend-adapter/
├── backend-adapter.py          # Точка входа
├── backend_adapter/            # Доменный пакет (11 модулей)
│   ├── config.py              # Парсинг env-переменных, multi-backend, YAML
│   ├── server.py              # HTTP-сервер, Handler
│   ├── convert.py             # Anthropic ↔ OpenAI конвертация
│   ├── streaming.py           # SSE streaming passthrough
│   ├── tracer.py              # JSONL trace-логирование, tool-use causality
│   ├── session_log.py         # Per-session логи с FIFO eviction
│   ├── skill.py               # Детекция скиллов по паттернам tool call arguments
│   ├── daemon.py              # Detach (double fork)
│   ├── logger.py              # Debug-логирование с redaction
│   ├── redact.py              # Маскирование секретов (токены, ключи)
│   └── __init__.py            # Module-level proxy
├── docs/
│   ├── architecture.md        # Архитектура
│   ├── environment.md         # Полный список env-переменных
│   ├── logging.md             # Конфигурация логирования
│   └── sanitizing.md          # Sanitization секретов
├── backend-adapter.service    # systemd unit (Linux, продакшен)
├── com.user.backend-adapter.plist  # launchd (macOS, продакшен)
├── backend-adapter.env        # Пример env для сервиса
├── sample.adapter.env         # Полный пример env (сервис + клиент)
├── adapter.yaml               # Пример multi-backend YAML
└── changelog.md               # История версий
```

---

## 3. Конфигурация

Все настройки задаются через переменные окружения. Минимальный набор зависит от режима работы.

### 3.1 Одный бэкенд (legacy-режим)

Для одного бэкенда задайте два обязательных параметра:

```bash
export ADAPTER_BACKEND_BASE="https://llm.service.example.com"
export ADAPTER_BACKEND_KEY="your-api-key-here"
```

### 3.2 Несколько бэкендов (multi-backend, рекомендуемый)

Создайте YAML-файл с конфигурацией:

```yaml
# adapter.yaml
backend:
  - name: home
    base: "http://127.0.0.1:8002"
    key: ADAPTER_HOME_KEY
  - name: litellm
    base: "https://llm.service.example.com"
    key: ADAPTER_LITELLM_KEY
```

Затем укажите путь к файлу:

```bash
export ADAPTER_BACKEND_CONFIG="./adapter.yaml"
```

Каждый бэкенд имеет три поля:

| Поле | Описание |
|---|---|
| `name` | Идентификатор бэкенда для префикса модели |
| `base` | Базовый URL без `/v1/` |
| `key` | Прямой токен **или** имя переменной окружения. Адаптер сначала подставляет значение переменной, затем использует как Bearer token |

**Маршрутизация**: если модель содержит префикс `<name>.` (например `kl.qwen3.6-35b-a3b`),
запрос отправляется на соответствующий бэкенд. Fallback — первый бэкенд в конфигурации.

### 3.3 Настройка клиента Claude Code

Для работы Claude Code через адаптер задайте:

```bash
# Точка подключения
export ANTHROPIC_BASE_URL="http://localhost:9999"

# Пустой ключ — адаптер сам обрабатывает авторизацию на бэкенде
export ANTHROPIC_API_KEY=""

# Отключить телеметрию и атрибутацию
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"
export CLAUDE_CODE_ATTRIBUTION_HEADER="0"

# Модель бэкенда (через маппинг, если нужно)
export ANTHROPIC_MODEL="qwen3.6-35b-a3b"
export ANTHROPIC_DEFAULT_MODEL="qwen3.6-35b-a3b"

# Отключить thinking (не все бэкенды поддерживают)
export CLAUDE_CODE_DISABLE_THINKING="1"
```

> **Примечание:** переменные `ANTHROPIC_*` и `CLAUDE_CODE_*` — настройки клиента Claude Code,
> а не адаптера. Адаптер их не читает, они задаются в окружении процесса Claude Code.
> Полный список переменных адаптера — в [`docs/environment.md`](environment.md).

### 3.4 Рекомендуемые параметры сети

```bash
# Таймаут запроса к бэкенду (сек)
export ADAPTER_TIMEOUT=300

# Количество повторов при таймауте / 429 / 502 / 503 / 504
# Задержка экспоненциальная: 2^attempt
export ADAPTER_RETRY_COUNT=3

# Порт адаптера (по умолчанию 9999)
export ADAPTER_PROXY_PORT=9999
```

### 3.5 Стриминг

```bash
# Включить SSE streaming passthrough
export ADAPTER_STREAMING_ENABLE=1

# Передавать токены usage в стриме (требуется от бэкенда stream_options.include_usage)
export ADAPTER_STREAM_INCLUDE_USAGE=1
```

Если бэкенд некорректно стримит, отключите стриминг полностью:
`ADAPTER_STREAMING_ENABLE=0` (аварийный режим — адаптер ждёт ответ целиком).

### 3.6 Валидация моделей

```bash
# Строгий режим: адаптер опрашивает бэкенд (/v1/models) и принимает только известные модели
export ADAPTER_STRICT_MODELS=1
# Разрешающий режим: любая модель принимается
# export ADAPTER_STRICT_MODELS=0
```

### 3.7 Маппинг моделей

При необходимости преобразования имён моделей:

```bash
export ADAPTER_MODELS_MAPPING="claude-sonnet-4-20250514:k2-05,claude-opus-4-20250514:k2-05-opus"
```

Формат: `agent_model:backend_model`. Применяется **до** валидации — преобразованная модель
проверяется по новому имени.

### 3.8 Отладка и трассировка

```bash
# Включить debug-логирование (1 = включено, 0/false/no = выключено)
export ADAPTER_DEBUG_ENABLE=1

# Путь к файлу логов (если задан, логи пишутся в файл вместо консоли)
# export ADAPTER_DEBUG_LOGFILE="/tmp/adapter.log"

# Максимальная длина trim-блоков (символы)
# export ADAPTER_DEBUG_TRIM=3000

# Переопределить PID-файл в detach-режиме
# export ADAPTER_PIDFILE="/tmp/adapter.pid"

# Тег-фильтры отладочных блоков (отключить trim для указанных тегов)
# export ADAPTER_DEBUG_TAGS_FULL="BODY,TOOL_RESULT,RESPONSE"

# JSON-дампы per-session (записывает полные тела в session_log/*.json)
# export ADAPTER_DEBUG_TAGS_JSON="TOOL_RESULT"

# Детальные логи результатов инструментов
# export ADAPTER_DEBUG_TOOLS=0
export ADAPTER_DEBUG_TOOLS_ERROR=1
```

**Трассировка (trace)** — структурированный JSONL-лог для каждого tool call и ответа:

```bash
# Путь к trace-файлу. Если указана директория — создаётся отдельный файл на сессию
# export ADAPTER_TRACE_LOGFILE="/tmp/adapter-trace.jsonl"

# Обрезка полей (0 = без обрезки)
# export ADAPTER_TRACE_REASONING_MAX_CHARS=0
# export ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS=0
```

**Санитайзер** — автоматическое маскирование чувствительных данных (токены, заголовники, ключи):

```bash
# 0 = санитайзер активен (по умолчанию), 1 = отключён (секреты в логах)
# export ADAPTER_SENSITIVE_LOGGING_ENABLE=0
```

**Детекция скиллов** — адаптер проверяет аргументы tool calls по regex-паттернам:

```bash
# Встроенные паттерны: devtools, frontmatter, klast, mytasks, prreview
# Путь к JSON-файлу с пользовательскими паттернами (по умолчанию встроены)
# export ADAPTER_SKILL_PATTERNS=""
```

Подробнее про логирование — в [`docs/logging.md`](logging.md).

### 3.9 Полный пример env-файла

```bash
# sample.adapter.env — полный пример
# ============================================================
# Adapter configuration (backend-adapter.py)
# ============================================================

# --- Backend connection ---
export ADAPTER_BACKEND_BASE="https://llm.service.example.com"
export ADAPTER_BACKEND_KEY="your-api-key-here"

# --- Server settings ---
export ADAPTER_PROXY_PORT=9999

# --- Network ---
export ADAPTER_TIMEOUT=300
export ADAPTER_RETRY_COUNT=3

# --- Streaming ---
export ADAPTER_STREAMING_ENABLE=1
export ADAPTER_STREAM_INCLUDE_USAGE=1

# --- Models ---
export ADAPTER_STRICT_MODELS=1
# export ADAPTER_MODELS_MAPPING="claude-sonnet:k2-05"

# --- Debugging ---
export ADAPTER_DEBUG_ENABLE=1
# export ADAPTER_DEBUG_LOGFILE="/tmp/adapter.log"
# export ADAPTER_DEBUG_TAGS_FULL="BODY,TOOL_RESULT"
# export ADAPTER_DEBUG_TOOLS_ERROR=1

# --- Tracing (structured JSONL log) ---
# export ADAPTER_TRACE_LOGFILE="/tmp/adapter-trace.jsonl"

# --- Sanitizer (secret masking in logs) ---
export ADAPTER_SENSITIVE_LOGGING_ENABLE=0

# --- Claude Code client ---
export ANTHROPIC_BASE_URL="http://localhost:9999"
export ANTHROPIC_API_KEY=""
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"
export CLAUDE_CODE_ATTRIBUTION_HEADER="0"
export ANTHROPIC_MODEL="qwen3.6-35b-a3b"
export ANTHROPIC_DEFAULT_MODEL="qwen3.6-35b-a3b"
export CLAUDE_CODE_DISABLE_THINKING="1"
```

---

## 4. Запуск

### 4.1 В foreground (разработка)

```bash
# Загрузить переменные
source sample.adapter.env
# или: source backend-adapter.env

# Запустить
python3 backend-adapter.py
```

Ожидаемый вывод:

```
======================================================================
Claude Code Adapter v0.6.9 (...
Listening:  http://localhost:9999
Trace log:  (disabled)
Models:     strict validation
Streaming:  enabled (SSE passthrough)

Backend:    https://llm.service.example.com/v1/chat/completions
Retries:    3
======================================================================
```

Сервер запущен, ждёт подключения Claude Code на порту 9999.

Остановить: `Ctrl+C` (SIGINT).

### 4.2 В фоне (detach-режим, для параллельной работы)

```bash
# Загрузить env
source sample.adapter.env

# Запустить в фоне
ADAPTER_DETACH_ENABLE=1 python3 backend-adapter.py
```

Detach-режим (double fork UNIX-daemon pattern):

- Родительский процесс завершается немедленно
- stdio/stderr перенаправлены в `/dev/null`
- PID-файл пишется в `/tmp/adapter.pid` (по умолчанию)
- Логи идут в `ADAPTER_DEBUG_LOGFILE`, если задан

Управление:

```bash
cat /tmp/adapter.pid     # прочитать PID
kill $(cat /tmp/adapter.pid)   # остановить
```

> **Важно:** detach-режим не предназначен для продакшен-использования.
> Для постоянной работы см. раздел 7 — systemd (Linux) или launchd (macOS).

### 4.3 Запуск Claude Code

После запуска адаптера, в другом терминале:

```bash
claude
```

Claude Code автоматически подхватит `ANTHROPIC_BASE_URL` из окружения и будет обращаться к
адаптеру на `localhost:9999` вместо Anthropic API.

---

## 5. Проверка

```bash
# Адаптер запущен и слушает порт
lsof -i :9999
# или
ss -tlnp | grep 9999

# В логах адаптера (если включён debug)
# должны быть строки "[INIT] Loaded N models from backend:"

# Попробовать запрос
curl -X POST http://localhost:9999/v1/messages \
  -H "Content-Type: application/json" \
  -H "Anthropic-Version: 2023-06-01" \
  -H "x-api-key: dummy" \
  -d '{"model":"qwen3.6-35b-a3b","messages":[{"role":"user","content":"Hi"}]}'
```

Если адаптер возвращает ответ (даже ошибку — не 500) — соединение работает.

---

## 6. Устранение неполадок

### Порт уже занят

```bash
lsof -i :9999
# Переключить на другой порт
ADAPTER_PROXY_PORT=9998 python3 backend-adapter.py
```

### Нет ответа от бэкенда

```bash
# Включить debug-логи
export ADAPTER_DEBUG_ENABLE=1
# или писать логи в файл
export ADAPTER_DEBUG_LOGFILE="/tmp/adapter.log"

# Проверить соединение с бэкендом напрямую
curl -s https://llm.service.example.com/v1/models
```

### Ошибка инициализации моделей

```
[FATAL] Failed to probe backend models at startup: ...
Adapter cannot start without knowing available models. Exiting.
```

Проверьте:

- Бэкенд доступен и отдаёт `GET /v1/models`
- `ADAPTER_BACKEND_KEY` содержит валидный токен
- `ADAPTER_STRICT_MODELS=0` — переключить в разрешающий режим

### Ошибка SSL

Если бэкенд использует самоподписанный сертификат:

- Проверьте, что `ssl.create_default_context()` корректно настроен
- Для тестов: используйте `http://` вместо `https://` (локальный бэкенд)

---

## 7. Переход в продакшен

Для постоянной работы адаптера используйте системные сервисы. Для разработки и тестов —
отладочный detach-режим (раздел 4.2).

### 7.1 Linux — systemd

Системные файлы: `backend-adapter.service` и `backend-adapter.env`.

**Минимальный набор env-переменных** для systemd-юнита:

| Переменная | Значение | Зачем |
|---|---|---|
| `ADAPTER_BACKEND_BASE` | URL бэкенда | Точка подключения к LLM |
| `ADAPTER_BACKEND_KEY` | API-ключ | Авторизация на бэкенде |
| `ADAPTER_PROXY_PORT` | `9999` | Порт, на котором слушает адаптер |
| `ADAPTER_DEBUG_ENABLE` | `1` | Включить логирование |
| `ADAPTER_DETACH_ENABLE` | `0` | **Важно:** не включаем detach при systemd |

> **Важно:** `ADAPTER_DETACH_ENABLE=0` при работе через systemd — systemd сам следит за процессом.
> Включать detach-режим (`ADAPTER_DETACH_ENABLE=1`) вместе с systemd нельзя:
> двойной форк отделит процесс от управления systemd, и `Restart=on-failure` не сработает.

**Установка:**

```bash
# 1. Скопировать юнит в директорию user-юнитов
cp backend-adapter.service ~/.config/systemd/user/backend-adapter.service

# 2. Заполнить .env (или создать новый файл для сервиса)
# ADAPTER_BACKEND_BASE="https://llm.service.example.com"
# ADAPTER_BACKEND_KEY="your-api-key-here"
# ADAPTER_PROXY_PORT=9999
# ADAPTER_DEBUG_ENABLE=1
# ADAPTER_DETACH_ENABLE=0

# 3. Загрузить изменения
systemctl --user daemon-reload

# 4. Включить автозапуск
systemctl --user enable backend-adapter.service

# 5. Запустить
systemctl --user start backend-adapter.service

# 6. Проверить статус
systemctl --user status backend-adapter.service
```

**Управление:**

```bash
# Статус
systemctl --user status backend-adapter.service

# Остановить
systemctl --user stop backend-adapter.service

# Перезапустить
systemctl --user restart backend-adapter.service

# Включить / отключить автозагрузку
systemctl --user enable  backend-adapter.service
systemctl --user disable  backend-adapter.service

# Логи в реальном времени
journalctl --user -u backend-adapter -f

# Логи за последний час
journalctl --user -u backend-adapter --since "1 hour ago"
```

**Почему systemd, а не detach?**

| detach (`ADAPTER_DETACH_ENABLE=1`) | systemd |
|---|---|
| Процесс живёт сам по себе | systemd следит за процессом |
| Нет рестарта при падении | `Restart=on-failure` |
| Нет логов | логи в `journalctl` |
| PID-файл вручную | systemd сам знает PID |
| Подходит для тестов | **подходит для продакшена** |

### 7.2 macOS — launchd

На macOS вместо systemd используется **launchd**. Системный файл:
`com.user.backend-adapter.plist`.

**Минимальный набор env-переменных** в `<key>EnvironmentVariables</key>`:

| Переменная | Значение |
|---|---|
| `ADAPTER_BACKEND_BASE` | URL бэкенда |
| `ADAPTER_BACKEND_KEY` | API-ключ |
| `ADAPTER_PROXY_PORT` | `9999` |
| `ADAPTER_DEBUG_ENABLE` | `1` |
| `ADAPTER_DETACH_ENABLE` | `0` |

**Установка:**

```bash
# 1. Скопировать plist в директорию user-задач
cp com.user.backend-adapter.plist \
   ~/Library/LaunchAgents/com.user.backend-adapter.plist

# 2. Загрузить задачу
launchctl load ~/Library/LaunchAgents/com.user.backend-adapter.plist

# 3. Проверить, что работает
launchctl list | grep backend-adapter
```

**Управление:**

```bash
# Загрузить (запустить)
launchctl load ~/Library/LaunchAgents/com.user.backend-adapter.plist

# Выгрузить (остановить)
launchctl unload ~/Library/LaunchAgents/com.user.backend-adapter.plist

# Перезагрузить (restart = unload + load)
launchctl unload  ~/Library/LaunchAgents/com.user.backend-adapter.plist
launchctl load    ~/Library/LaunchAgents/com.user.backend-adapter.plist

# Проверить статус (ищет по Label)
launchctl list | grep com.user.backend-adapter

# Проверить PID процесса
pgrep -f backend-adapter.py

# Посмотреть логи (StandardOutPath из plist)
tail -f ~/tmp/adapter.log
```

**Почему launchd, а не detach?**

| detach (`ADAPTER_DETACH_ENABLE=1`) | launchd |
|---|---|
| Процесс живёт сам по себе | launchd следит за процессом |
| Нет рестарта при падении | авто-рестарт через `KeepAlive` |
| Нет логов | логи в `StandardOutPath` |
| PID-файл вручную | launchd сам знает PID |
| Подходит для тестов | **подходит для продакшена** |

---

## Ссылки

- [`docs/environment.md`](environment.md) — полный список всех env-переменных
- [`docs/logging.md`](logging.md) — конфигурация логирования и trace
- [`docs/architecture.md`](architecture.md) — архитектура и диаграмма компонентов
- [`docs/sanitizing.md`](sanitizing.md) — правила санитизации секретов
- [GitHub](https://github.com/alekseybb197/backend-adapter) — исходный репозиторий
- [`changelog.md`](../changelog.md) — история версий
