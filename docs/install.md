# Установка — backend-adapter

> **backend-adapter** (v0.7.2) — HTTP-прокси-адаптер, позволяющий использовать **Claude Code** (CLI)
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

Единственная внешняя зависимость — **PyYAML** (используется в session-логировании);
остальной код — стандартная библиотека Python. Установка зависимостей:
`pip install -r requirements.txt`.

---

## 1. Требования

- **Python 3.10+** (код использует аннотации `X | Y`; помимо стандартной библиотеки
  требуется только `PyYAML`)
- **git** (для клонирования репозитория)
- **bash/zsh** (для запуска через терминал)

---

## 2. Тесты

Тестовый каркас — `pytest`, покрывает все модули адаптера (чистая логика +
интеграция HTTP-сервера). Тесты не требуют сети, бэкендов и ключей: бэкенд
эмулируется фейковым HTTP-сервером внутри тестов.

Установка (первый раз):

```bash
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt   # = requirements.txt + pytest
```

Запуск:

```bash
venv/bin/pytest            # все тесты, краткий вывод
venv/bin/pytest -v         # подробно, по одному тесту на строку
```

Критерии приёма:

- **0 падений** (сейчас: 138 тестов);
- запуск из чистого состояния — `git clean -xdf` не требуется, тесты
  самодостаточны;
- в рабочей директории и в `tmp/` не появляются файлы от тестов
  (изоляция логов — фикстура `isolate_logs`);
- интеграционные тесты поднимают фейковый бэкенд на случайном порту
  (`ADAPTER_PROXY_PORT` в тестах — **9998**, порт 9999 не используется).

---

## 3. Клонирование

```bash
# Клонировать репозиторий
git clone https://github.com/alekseybb197/backend-adapter.git
cd backend-adapter

# Установить зависимости (единственная внешняя — PyYAML).
# При необходимости используйте виртуальное окружение:
#   python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Структура проекта:

```
backend-adapter/
├── backend-adapter.py          # Точка входа
├── backend_adapter/            # Доменный пакет (20 модулей, включая __init__.py; artifact_tree* — 8 модулей)
│   ├── config.py              # Парсинг env, конфиг бэкендов (YAML), модели
│   ├── server.py              # HTTP-сервер, Handler
│   ├── convert.py             # Anthropic ↔ [OI] конвертация
│   ├── streaming.py           # SSE streaming passthrough
│   ├── tracer.py              # JSONL trace-логирование, tool-use causality
│   ├── session_log.py         # Per-session логи с FIFO eviction
│   ├── skill.py               # Детекция скиллов по паттернам tool call arguments
│   ├── daemon.py              # Detach (double fork)
│   ├── logger.py              # Debug-логирование с redaction
│   ├── redact.py              # Маскирование секретов (токены, ключи)
│   ├── webserver.py           # WEBUI-ядро: общий веб-сервер, роутинг эндпойнтов, CLI
│   ├── webui_status.py        # WEBUI-эндпойнт "/": статус (версия, LLM, модели)
│   ├── session_viewer.py      # WEBUI-эндпойнт "/session": просмотр *.parts сессий
│   ├── artifact_tree.py       # artifact_tree*: публичный API (generate())
│   ├── artifact_tree_common.py    # утилиты, константы, цвета
│   ├── artifact_tree_registry.py  # реестр артефактов + дедупликация
│   ├── artifact_tree_parse.py     # разбор дампов, классификация kind
│   ├── artifact_tree_turnbuilder.py  # связывание openai_body/fetch_raw в ходы
│   ├── artifact_tree_plantuml.py  # PlantUML-рендер
│   ├── artifact_tree_graphviz.py  # PNG через plantuml/graphviz-fallback
│   ├── artifact_tree_html.py      # интерактивный tree.html
│   └── __init__.py            # Module-level proxy
│   ├── architecture.md        # Архитектура
│   ├── environment.md         # Полный список env-переменных
│   ├── logging.md             # Конфигурация логирования
│   └── sanitizing.md          # Sanitization секретов
├── backend-adapter.service    # systemd unit (Linux, продакшен)
├── com.user.backend-adapter.plist  # launchd (macOS, продакшен)
├── backend-adapter.env        # Пример env для сервиса
├── sample.adapter.env         # Полный пример env (сервис + клиент)
├── sample.adapter.yaml         # Пример YAML-конфига бэкендов
├── requirements.txt           # Зависимости (единственная — PyYAML)
└── changelog.md               # История версий
```

---

## 4. Standalone-бинарники

Для каждого релиза публикуются готовые исполняемые файлы (PyInstaller
`--onefile`), которые **не требуют Python или PyYAML** на целевой машине:
внутри бинарника упакован интерпретатор и все зависимости. Весь конфиг —
как обычно, через переменные окружения (раздел [5](#5-конфигурация)).

### 4.1 Где скачать

Зайдите на страницу [Releases](https://github.com/alekseybb197/backend-adapter/releases)
и скачайте бинарник под вашу платформу:

| Платформа | Файл | Размер |
|---|---|---|
| Linux x64 | `backend-adapter-linux-x64` | ~15 МБ |
| Linux ARM64 | `backend-adapter-linux-arm64` | ~15 МБ |
| macOS ARM64 (Apple Silicon) | `backend-adapter-macos-arm64` | ~15 МБ |
| macOS Intel | `backend-adapter-macos-x64` | ~15 МБ |
| Windows x64 | `backend-adapter-windows-x64.exe` | ~15 МБ |

### 4.2 Запуск

Бинарнику нужен тот же конфиг, что и исходникам: YAML-файл бэкендов через
`ADAPTER_BACKEND_CONFIG` (см. [5.1](#51-конфигурация-бэкенда-yaml-файл-через-adapter_backend_config))
плюс env-переменная токена из поля `key`. Скачайте `sample.adapter.yaml`
из репозитория, заполните и укажите на него:

```yaml
# adapter.yaml — один бэкенд; другие добавляются записями в тот же список
backend:
  - name: llm-service
    base: https://llm.service.example.com
    key: ADAPTER_BACKEND_KEY_LLM_SERVICE   # имя env-переменной токена
```

**Linux / macOS (bash):**

```bash
# Токен: имя переменной из поля key в YAML
export ADAPTER_BACKEND_KEY_LLM_SERVICE="sk-..."
export ADAPTER_BACKEND_CONFIG="/path/to/adapter.yaml"
chmod +x backend-adapter-linux-x64        # только Linux; на macOS права обычно уже стоят
./backend-adapter-linux-x64
```

**Windows (PowerShell):**

```powershell
$env:ADAPTER_BACKEND_KEY_LLM_SERVICE="sk-..."
$env:ADAPTER_BACKEND_CONFIG="C:\path\to\adapter.yaml"
.\backend-adapter-windows-x64.exe
```

**Ограничения Windows:** `ADAPTER_DETACH_ENABLE=1` не поддержан (режим detach
использует `os.fork`, которого нет на Windows). Запускайте бинарник в
foreground; для работы в фоне используйте фоновую задачу PowerShell или окно
консоли. Остальные переменные работают одинаково на всех платформах.

Дефолты не отличаются от исходников (zero-config): debug-блоки видны в консоли,
на диск ничего не пишется, пока не задан `ADAPTER_DEBUG_LOGPATH`, статус-страница
WEBUI доступна на `http://127.0.0.1:8765/`.

Когда стоит предпочесть исходники (`git clone` + `pip install -r
requirements.txt` или `./scripts/run.sh`): бинарник собирается под конкретную
ОС/архитектуру и не подходит, если нужен нестандартный Python, свои правки
кода или запуск на платформе вне таблицы выше.

### 4.3 Локальная сборка

PyInstaller не кросскомпилирует — бинарник собирается **на той же
платформе**, для которой предназначен. Для локальной сборки (в виртуальном
окружении проекта):

```bash
venv/bin/pip install pyinstaller
./scripts/build-binaries.sh            # платформа определяется автоматически
# или явно:
./scripts/build-binaries.sh macos-arm64
# Результат: dist/binaries/<target>/backend-adapter
```

Ручной вариант той же команды:

```bash
pyinstaller --onefile \
  --name backend-adapter \
  --hidden-import yaml \
  --hidden-import yaml.emitter \
  backend-adapter.py
```

Точка входа сборки — `backend-adapter.py` (консольная команда
`backend-adapter`, генерируемая при `pip install .` из `backend_adapter/cli.py`,
исполняет этот же скрипт через runpy — в бинарнике он уже является кодом
запуска).

Все четыре артефакта (Linux x64, macOS ARM64/x64, Windows x64) для релизов
собираются автоматически в CI при push тега `v*` — см.
`.github/workflows/release-binaries.yml`.

---

## 5. Конфигурация

Все настройки задаются через переменные окружения.

### 5.1 Конфигурация бэкенда (YAML-файл через ADAPTER_BACKEND_CONFIG)

Единственный способ указать подключение к бэкенду — переменная окружения
`ADAPTER_BACKEND_CONFIG`, ссылающаяся на YAML-файл со структурой `backend:`
(пример — `sample.adapter.yaml` в корне репозитория). Один или несколько бэкендов
задаются записями в одном списке:

```yaml
# sample.adapter.yaml — один бэкенд; другие добавляются записями в тот же список
backend:
  - name: home
    base: "http://127.0.0.1:8002"
    key: ADAPTER_HOME_KEY
  - name: assistant
    base: "https://llm.service.example.com"
    key: ADAPTER_LITELLM_KEY
```

Укажите путь к файлу в окружении:

```bash
export ADAPTER_BACKEND_CONFIG="./sample.adapter.yaml"
```

Каждый бэкенд имеет три поля:

| Поле | Описание |
|---|---|
| `name` | Идентификатор бэкенда для префикса модели |
| `base` | Базовый URL без `/v1/` |
| `key` | Прямой токен **или** имя переменной окружения. Адаптер сначала подставляет значение переменной, затем использует как Bearer token |

**Маршрутизация**: если модель содержит префикс `<name>.` (например `kl.qwen3.6-35b-a3b`),
запрос отправляется на соответствующий бэкенд. Fallback — первый бэкенд в конфигурации.

### 5.2 Настройка клиента Claude Code

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

### 5.3 Рекомендуемые параметры сети

```bash
# Таймаут запроса к бэкенду (сек)
export ADAPTER_TIMEOUT=300

# Количество повторов при таймауте / 429 / 502 / 503 / 504
# Задержка экспоненциальная: 2^attempt
export ADAPTER_RETRY_COUNT=3

# Порт адаптера (по умолчанию 9999)
export ADAPTER_PROXY_PORT=9999

# Адрес, на котором слушает адаптер (default: localhost; 0.0.0.0 — все интерфейсы)
# export ADAPTER_ENDPOINT_HOST="127.0.0.1"
```

### 5.4 Стриминг

```bash
# Включить SSE streaming passthrough
export ADAPTER_STREAMING_ENABLE=1

# Передавать токены usage в стриме (требуется от бэкенда stream_options.include_usage)
export ADAPTER_STREAM_INCLUDE_USAGE=1
```

Если бэкенд некорректно стримит, отключите стриминг полностью:
`ADAPTER_STREAMING_ENABLE=0` (аварийный режим — адаптер ждёт ответ целиком).

### 5.5 Валидация моделей

```bash
# Строгий режим: адаптер опрашивает бэкенд (/v1/models) и принимает только известные модели
export ADAPTER_STRICT_MODELS=1
# Разрешающий режим: любая модель принимается
# export ADAPTER_STRICT_MODELS=0
```

### 5.6 Маппинг моделей

При необходимости преобразования имён моделей:

```bash
export ADAPTER_MODELS_MAPPING="claude-sonnet-4-20250514:k2-05,claude-opus-4-20250514:k2-05-opus"
```

Формат: `agent_model:backend_model`. Применяется **до** валидации — преобразованная модель
проверяется по новому имени.

### 5.7 Отладка и трассировка

```bash
# Мастер-выключатель логирования: 1 — логи сессий/трейсы/дампы пишутся
# (0/false/no — ничего не пишется, папка не создаётся)
export ADAPTER_DEBUG_ENABLE=1

# Директория логов сессий: debug-логи (session-*.log), trace-логи (session-*.jsonl),
# *.parts дампы. Пусто (дефолт) — файловая запись выключена: диск не используется,
# папка не создаётся; консольные debug-блоки видны при ADAPTER_DEBUG_ENABLE=1.
# Задана — папка создаётся при необходимости.
# export ADAPTER_DEBUG_LOGPATH="/tmp/adapter-logs"

# Максимальная длина trim-блоков (символы)
# export ADAPTER_DEBUG_TRIM=3000

# Переопределить PID-файл в detach-режиме
# export ADAPTER_PIDFILE="/tmp/adapter.pid"

# Тег-фильтры отладочных блоков (отключить trim для указанных тегов)
# export ADAPTER_DEBUG_TAGS_FULL="BODY,TOOL_RESULT,RESPONSE"

# JSON/YAML-дампы per-session всех частей протокола (требуют ADAPTER_DEBUG_ENABLE=1
# и директорию ADAPTER_DEBUG_LOGPATH)
# export ADAPTER_DEBUG_TAGS_OUT=1

# Веб-интерфейс: / — статус (версия, LLM-эндпойнты, модели), /session — просмотр сессий.
# Включён ПО УМОЛЧАНИЮ (ADAPTER_WEBUI_ENABLE=1) на 127.0.0.1:8765 — статус-страница
# доступна сразу; корень — ADAPTER_DEBUG_LOGPATH (если задан, там *.parts сессии),
# иначе ./tmp/webui (вкладка /session пуста). Отключить: ADAPTER_WEBUI_ENABLE=0.
# export ADAPTER_WEBUI_PORT=8765
# export ADAPTER_WEBUI_HOST="127.0.0.1"
# Standalone-запуск вне процесса адаптера: python -m backend_adapter.webserver [ROOT] [--port] [--host]

# Детальные логи результатов и ошибок инструментов (оба по умолчанию 0 — выкл,
# zero-config не обрабатывает собранные логи; включите при отладке инструментов)
# export ADAPTER_DEBUG_TOOLS=1
# export ADAPTER_DEBUG_TOOLS_ERROR=1
```

**Трассировка (trace)** — структурированный JSONL-лог для каждого tool call и ответа:

```bash
# Trace-логи (session-*.jsonl) пишутся в ту же директорию ADAPTER_DEBUG_LOGPATH,
# что и debug-логи; включаются тем же мастер-выключателем ADAPTER_DEBUG_ENABLE=1.
# Отдельной переменной пути нет.

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

### 5.8 Полный пример env-файла

```bash
# sample.adapter.env — полный пример
# ============================================================
# Adapter configuration (backend-adapter.py)
# ============================================================

# --- Backend connection ---
# Путь к YAML-файлу конфигурации бэкендов — единственный способ
# указать подключение. Пример файла — sample.adapter.yaml в корне
# репозитория (структура backend: - name/base/key; key — имя
# переменной окружения, в которой лежит токен).
export ADAPTER_BACKEND_CONFIG="<путь>/sample.adapter.yaml"

# --- Server settings ---
export ADAPTER_PROXY_PORT=9999
# export ADAPTER_ENDPOINT_HOST="127.0.0.1"   # адрес, на котором слушает адаптер (default: localhost; 0.0.0.0 — все интерфейсы)

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
# export ADAPTER_DEBUG_LOGPATH="/tmp/adapter-logs"   # директория логов сессий (пусто — файловая запись выключена)
# export ADAPTER_DEBUG_TAGS_FULL="BODY,TOOL_RESULT"
# export ADAPTER_DEBUG_TOOLS_ERROR=1

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

## 6. Запуск

### 6.1 В foreground (разработка)

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
Claude Code Adapter v0.7.2 (...
Listening:  http://127.0.0.1:9999
Logs:       console only (ADAPTER_DEBUG_ENABLE=1; диск: задайте ADAPTER_DEBUG_LOGPATH)
Models:     strict validation
Streaming:  enabled (SSE passthrough)

Backends:   1 configured:
  - home: http://127.0.0.1:8002
[WEBUI] http://127.0.0.1:8765/ (root: ./tmp/webui)
======================================================================
```

Сервер запущен, ждёт подключения Claude Code на порту 9999.

Остановить: `Ctrl+C` (SIGINT).

### 6.2 В фоне (detach-режим, для параллельной работы)

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
- Логи идут в директорию `ADAPTER_DEBUG_LOGPATH`, если задана
  (пусто — файловая запись выключена, только консоль)

Управление:

```bash
cat /tmp/adapter.pid     # прочитать PID
kill $(cat /tmp/adapter.pid)   # остановить
```

> **Важно:** detach-режим не предназначен для продакшен-использования.
> Для постоянной работы см. раздел 9 — systemd (Linux) или launchd (macOS).

### 6.3 Запуск Claude Code

После запуска адаптера, в другом терминале:

```bash
claude
```

Claude Code автоматически подхватит `ANTHROPIC_BASE_URL` из окружения и будет обращаться к
адаптеру на `localhost:9999` вместо Anthropic API.

---

## 7. Проверка

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

## 8. Устранение неполадок

### Порт уже занят

```bash
lsof -i :9999
# Переключить на другой порт
ADAPTER_PROXY_PORT=9998 python3 backend-adapter.py
```

### Нет ответа от бэкенда

```bash
# Включить debug-логи (консольные блоки видны при дефолте ADAPTER_DEBUG_ENABLE=1)
# или направить логи в директорию на диск:
# export ADAPTER_DEBUG_LOGPATH="/tmp/adapter-logs"

# Проверить соединение с бэкендом напрямую
curl -s https://llm.service.example.com/v1/models
```

### Ошибка инициализации моделей

```
[FATAL] Failed to initialize backends: <причина>
Adapter cannot start. Exiting.
```

Проверьте:

- Путь в `ADAPTER_BACKEND_CONFIG` корректен и YAML-файл существует
- Структура YAML — `backend:` со списком записей (пример — `sample.adapter.yaml`)
- Поле `key` каждой записи указывает на существующую переменную окружения, в которой лежит токен
- Бэкенд доступен и отдаёт `GET /v1/models`
- `ADAPTER_STRICT_MODELS=0` — переключить в разрешающий режим

### Ошибка SSL

Если бэкенд использует самоподписанный сертификат:

- Проверьте, что `ssl.create_default_context()` корректно настроен
- Для тестов: используйте `http://` вместо `https://` (локальный бэкенд)

---

## 9. Переход в продакшен

Для постоянной работы адаптера используйте системные сервисы. Для разработки и тестов —
отладочный detach-режим (раздел 6.2).

### 9.1 Linux — systemd

Системные файлы: `backend-adapter.service` и `backend-adapter.env`.

**Минимальный набор env-переменных** для systemd-юнита:

| Переменная | Значение | Зачем |
|---|---|---|
| `ADAPTER_BACKEND_CONFIG` | путь к YAML | Подключение к бэкенду (пример — `sample.adapter.yaml`) |
| `ADAPTER_PROXY_PORT` | `9999` | Порт, на котором слушает адаптер |
| `ADAPTER_ENDPOINT_HOST` | `127.0.0.1` | Адрес, на котором слушает адаптер (только локально; `0.0.0.0` — все интерфейсы) |
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
# ADAPTER_BACKEND_CONFIG="/home/username/backend-adapter/sample.adapter.yaml"
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

### 9.2 macOS — launchd

На macOS вместо systemd используется **launchd**. Системный файл:
`com.user.backend-adapter.plist`.

**Минимальный набор env-переменных** в `<key>EnvironmentVariables</key>`:

| Переменная | Значение |
|---|---|
| `ADAPTER_BACKEND_CONFIG` | путь к YAML (пример — `sample.adapter.yaml`) |
| `ADAPTER_PROXY_PORT` | `9999` |
| `ADAPTER_ENDPOINT_HOST` | `127.0.0.1` |
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
