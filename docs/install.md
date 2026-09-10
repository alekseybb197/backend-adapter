# Установка — backend-adapter

> **backend-adapter** (v0.9.2) — HTTP-прокси-адаптер, позволяющий использовать **Claude Code** (CLI)
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

### 2.1 Тест установщика (molecule)

`install.sh` — bash-скрипт, поэтому `pytest` его не покрывает. Для него
заведён отдельный **molecule**-сценарий `molecule/install/`: он поднимает
контейнер **ubuntu 24.04**, прогоняет `install.sh` и проверяет контракт
установки — бинарник скачивается, кладётся в `/usr/local/bin`, исполняем и
работоспособен.

Область первой итерации: только **Linux** и только **установка latest-бинарника**
(без `--service`). Сети нет: `curl` подменяется PATH-стабом
(`molecule/install/fixtures/curl`), который отдаёт фикстурный бинарник и
записывает запрошенный URL — тест ассертит, что URL в точности равен
`https://github.com/alekseybb197/backend-adapter/releases/latest/download/backend-adapter-<platform>`.

molecule в `requirements-dev.txt` не входит (нужен только для этого
сценария) — ставьте его в **отдельный** venv:

```bash
python3.12 -m venv tmp/molecule-venv
tmp/molecule-venv/bin/pip install -r molecule/requirements-molecule.txt

cd <repo>
PATH="$PWD/tmp/molecule-venv/bin:$PATH" tmp/molecule-venv/bin/molecule test -s install
```

`PATH=` с venv-каталогом впереди обязателен: иначе molecule может подхватить
системный `ansible-galaxy` другой версии. Сценарий называется `install`
(не `default`) — всегда указывайте `-s install`. Требуется запущенный
Docker. Быстрый цикл: `molecule converge -s install` / `verify` / `login` /
`destroy`.

В CI этот сценарий гоняется отдельным job `install-molecule`
(`.github/workflows/ci.yml`).

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

# Подготовить конфиги из примеров (docs/samples/) и заполнить их
cp docs/samples/sample.adapter.env  adapter.env
cp docs/samples/sample.adapter.yaml adapter.yaml
```

Структура проекта:

```
backend-adapter/
├── backend-adapter.py          # Точка входа
├── backend_adapter/            # Доменный пакет (27 модулей, включая __init__.py; artifact_tree* — 8 модулей)
│   ├── config.py              # Парсинг env, конфиг бэкендов (YAML), модели
│   ├── server.py              # HTTP-сервер, Handler
│   ├── convert.py             # Anthropic ↔ [OI] конвертация
│   ├── streaming.py           # SSE streaming passthrough
│   ├── tracer.py              # JSONL trace-логирование, tool-use causality
│   ├── session_log.py         # Per-session логи с FIFO eviction
│   ├── daemon.py              # Detach (double fork)
│   ├── logger.py              # Debug-логирование с redaction
│   ├── redact.py              # Маскирование секретов (токены, ключи)
│   ├── webserver.py           # WEBUI-ядро: общий веб-сервер, роутинг эндпойнтов, CLI
│   ├── webui_status.py        # WEBUI-эндпойнт "/": статус (версия, LLM, модели)
│   ├── webui_ops.py           # WEBUI health-эндпоинты "/healthz" "/health" "/live" "/ready"
│   ├── webui_config_api.py    # WEBUI-эндпойнт "/config": runtime-пул debug-переменных
│   ├── prometheus_exporter.py # Prometheus-метрики /metrics (отдельный слушатель, stdlib-only)
│   ├── session_viewer.py      # WEBUI-эндпойнт "/session": просмотр *.parts сессий
│   ├── probe_json.py          # JSON-результаты проверок бэкендов в LOGPATH
│   ├── artifact_tree.py       # artifact_tree*: публичный API (generate())
│   ├── artifact_tree_common.py    # утилиты, константы, цвета
│   ├── artifact_tree_registry.py  # реестр артефактов + дедупликация
│   ├── artifact_tree_parse.py     # разбор дампов, классификация kind
│   ├── artifact_tree_turnbuilder.py  # связывание openai_body/fetch_raw в ходы
│   ├── artifact_tree_plantuml.py  # PlantUML-рендер
│   ├── artifact_tree_graphviz.py  # PNG через plantuml/graphviz-fallback
│   ├── artifact_tree_html.py      # интерактивный tree.html
│   └── __init__.py            # Module-level proxy
├── install.sh                   # Однострочный установщик (curl | bash)
├── requirements.txt             # Зависимости (единственная — PyYAML)
├── scripts/
│   ├── build-binaries.sh        # Сборка standalone-бинарников (PyInstaller)
│   └── dev-run.sh               # Запуск из исходников (venv + конфиг)
├── molecule/                    # molecule-сценарий установщика (см. раздел 2):
│   ├── requirements-molecule.txt   #   зависимости molecule (отдельный venv)
│   └── install/                    #   сценарий "install" (Linux, latest, ubuntu 24.04)
├── docs/
│   ├── install.md               # Установка (этот файл)
│   ├── environment.md           # Полный список env-переменных
│   ├── logging.md               # Конфигурация логирования
│   ├── sanitizing.md            # Sanitization секретов
│   ├── webui.md                 # Руководство по WEBUI и API
│   ├── architecture.md          # Архитектура
│   ├── samples/                 # Примеры конфигов (см. раздел 3):
│   │   ├── sample.adapter.env   #   Полный пример env (все переменные адаптера)
│   │   ├── sample.adapter.yaml  #   Пример YAML-конфига бэкендов
│   │   ├── backend-adapter.service      #   systemd unit (Linux, из исходников)
│   │   └── com.user.backend-adapter.plist  # launchd (macOS, из исходников)
│   └── claude_code/             # Локальные настройки клиента [CC] (не для продакшена)
└── changelog.md                 # История версий
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

### 4.2 Установка одной строкой (curl | bash)

Быстрая установка бинарника с GitHub Releases — однострочным установщиком
`install.sh` из корня репозитория. Скрипт сам определяет ОС/архитектуру,
скачивает бинарник последнего релиза и кладёт его в **фиксированный для
платформы** путь: Linux → `/usr/local/bin` (при отсутствии прав — через
`sudo`), macOS → `~/.local/bin` (per-user, без `sudo`):

```bash
curl -fsSL https://raw.githubusercontent.com/alekseybb197/backend-adapter/main/install.sh | bash
```

> **macOS: Gatekeeper (quarantine).** Скачанный браузером или curl бинарник
> помечается атрибутом `com.apple.quarantine`; при первом запуске система заблокирует его
> («damaged» / «cannot be opened because the developer cannot be verified»). Снять атрибут:
>
> ```bash
> xattr -d com.apple.quarantine /usr/local/bin/backend-adapter
> ```
>
> Атрибут ставится при скачивании; у файла, уже снявшего его, — команда вернёт ошибку
> `No such xattr`, это нормально. Дальше запускайте обычным способом (раздел 4.3).

Установка бинарника **и** сервиса автозапуска одной командой:

```bash
# Linux: user-юнит systemd (systemctl --user); macOS: launchd-агент
curl -fsSL https://raw.githubusercontent.com/alekseybb197/backend-adapter/main/install.sh | bash -s -- --service
```

`--service` **не запускает** сервис сразу: он пишет свежий user-level юнит
(указывающий на установленный бинарник), env-файл со значениями по
умолчанию и пустым `ADAPTER_BACKEND_CONFIG` и включает автозапуск. После
установки заполните `ADAPTER_BACKEND_CONFIG` (в env-файле на Linux / в
`EnvironmentVariables` plist на macOS — launchd не читает env-файлы) и
запустите сервис вручную. Системные шаблоны репозитория
(`docs/samples/backend-adapter.service`,
`docs/samples/com.user.backend-adapter.plist`) рассчитаны на запуск из
исходников; для бинарника юнит генерируется установщиком.

Опции:

| Опция | Действие |
|---|---|
| `--service` | Дополнительно сгенерировать и включить сервис автозапуска: user-юнит systemd (Linux) / launchd-агент (macOS). Сервис **не запускается** сразу — сначала заполните `ADAPTER_BACKEND_CONFIG` (см. ниже) |
| `--help` | Показать справку |

Каталог установки фиксирован per-platform и **не переопределяется**: Linux →
`/usr/local/bin`, macOS → `~/.local/bin`. Установка — только бинарник
последнего релиза; сценарии `--pip` (из исходников) и `--prefix` (свой
каталог) удалены — для установки из исходников используйте `git clone` +
venv (раздел [3](#3-клонирование)). Включить сервис можно и переменной
окружения `SERVICE_INSTALL=1` (эквивалент `--service`).

> **Windows.** Bash-установщик Windows не поддерживает: он завершится с
> явной ошибкой и ссылкой на будущий PowerShell-установщик (`install.ps1`)
> или WSL. В WSL платформа определяется как Linux.

> **Security note.** Установщик общается только с github.com (официальные
> релизы и файлы этого репозитория); установка и сервис — в пределах
> текущего пользователя (user-level systemd/launchd, без root).
> Перед выполнением просмотрите скрипт:
> `curl -fsSL https://raw.githubusercontent.com/alekseybb197/backend-adapter/main/install.sh | less`

Установленному бинарнику нужен тот же конфиг, что и исходникам (см. ниже,
раздел [4.3](#43-запуск)): YAML-файл бэкендов через `ADAPTER_BACKEND_CONFIG`
плюс env-переменная токена из поля `key`. Примеры конфигов — в
`docs/samples/` репозитория (`sample.adapter.yaml`, `sample.adapter.env`);
скачайте и заполните их.

### 4.3 Запуск

Бинарнику нужен тот же конфиг, что и исходникам: YAML-файл бэкендов через
`ADAPTER_BACKEND_CONFIG` (см. [5.1](#51-конфигурация-бэкенда-yaml-файл-через-adapter_backend_config))
плюс env-переменная токена из поля `key`. Возьмите пример
`docs/samples/sample.adapter.yaml`, заполните и укажите на него:

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

Если установщик клал бинарник в `/usr/local/bin` (или `~/.local/bin`) и запуск из этой
папки падает с ошибкой Gatekeeper — снимите атрибут quarantine с установленного файла:

```bash
xattr -d com.apple.quarantine /usr/local/bin/backend-adapter   # или ~/.local/bin/backend-adapter
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
WEBUI доступна на `http://127.0.0.1:8765/`. Руководство по страницам и
JSON-эндпоинтам (включая таблицу использованных моделей и файл
`model-usage.yaml`) — `docs/webui.md`.

Когда стоит предпочесть исходники (`git clone` + `pip install -r
requirements.txt` или `./scripts/dev-run.sh`): бинарник собирается под конкретную
ОС/архитектуру и не подходит, если нужен нестандартный Python, свои правки
кода или запуск на платформе вне таблицы выше.

### 4.4 Локальная сборка

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
(пример — `docs/samples/sample.adapter.yaml`). Один или несколько бэкендов
задаются записями в одном списке:

```yaml
# sample.adapter.yaml — один бэкенд; другие добавляются записями в тот же список
# (полный пример — docs/samples/sample.adapter.yaml)
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
# Мастер-выключатель ФАЙЛОВОЙ записи: 1 — логи сессий/трейсы/дампы пишутся
# на диск (0/false/no — на диск ничего не пишется; консольные debug-логи
# безусловны и от флага не зависят). Дефолт 0.
export ADAPTER_DEBUG_ENABLE=0

# Директория логов сессий и корень WEBUI: debug-логи (session-*.log),
# trace-логи (session-*.jsonl), *.parts дампы, model-usage.yaml. Путь всегда
# непуст — при незаданной/пустой env дефолт ./tmp/logs (создаётся при старте);
# файлы в неё пишутся только при ADAPTER_DEBUG_ENABLE=1. Исключение — файлы
# инцидентов session-*.err (v0.9.0): пишутся в ту же директорию БЕЗУСЛОВНО при
# финальном ответе клиенту 4xx/5xx реального прокси-запроса (полные запрос и
# ошибка, без обрезки по TRIM; redact по умолчанию, полные данные при
# ADAPTER_SENSITIVE_LOGGING_ENABLE=1).
# export ADAPTER_DEBUG_LOGPATH="/tmp/adapter-logs"

# Максимальная длина КОНСОЛЬНЫХ debug-строк (символы; 0 — без обрезки).
# Файловый канал (session-*.log при ADAPTER_DEBUG_ENABLE=1) trim НЕ использует —
# пишет полные строки (v0.8.6-реформа)
# export ADAPTER_DEBUG_TRIM=3000

# Имя PID-файла в detach-режиме (v0.9.0): файл кладётся в
# ADAPTER_DEBUG_LOGPATH (basename значения; дефолт — adapter.pid).
# export ADAPTER_PIDFILE="adapter.pid"

# JSON/YAML-дампы per-session ВСЕХ логгируемых частей протокола (BODY,
# TOOL_RESULT, OPENAI_BODY, FETCH_RAW, RESPONSE — .json и .yaml парой;
# требуют ADAPTER_DEBUG_ENABLE=1 и директорию ADAPTER_DEBUG_LOGPATH)
# export ADAPTER_DEBUG_PARTS=1

# Веб-интерфейс: / — статус (версия, LLM-эндпойнты, модели), /session — просмотр сессий.
# Поднимается ВСЕГДА (v0.8.6; флага ADAPTER_WEBUI_ENABLE больше нет) на 127.0.0.1:8765 —
# статус-страница доступна сразу; корень — ADAPTER_DEBUG_LOGPATH (дефолт ./tmp/logs,
# там *.parts сессии и model-usage.yaml — таблица использованных моделей).
# Health-check для оркестрации: /healthz, /health, /live, /ready
# (см. docs/webui.md). Руководство по страницам и API — docs/webui.md.
# export ADAPTER_WEBUI_PORT=8765
# export ADAPTER_WEBUI_HOST="127.0.0.1"
# Standalone-запуск вне процесса адаптера: python -m backend_adapter.webserver [ROOT] [--port] [--host]

# Prometheus-экспортёр: ОТДЕЛЬНЫЙ слушатель метрик на ADAPTER_EXPORTER_PORT
# (дефолт 9100) и ADAPTER_WEBUI_HOST. Включён ПО УМОЛЧАНИЮ вместе с WEBUI
# (ADAPTER_EXPORTER_ENABLE=1); /metrics — текст text exposition 0.0.4 (stdlib-only,
# без библиотек). Отключить: ADAPTER_EXPORTER_ENABLE=0.
# export ADAPTER_EXPORTER_PORT=9100

# (Прежние «селекторы подробности» — ADAPTER_DEBUG_TOOLS /
# ADAPTER_DEBUG_TOOLS_ERROR / ADAPTER_DEBUG_TAGS_FULL — удалены в v0.8.6:
# консоль всегда печатается с обрезкой ADAPTER_DEBUG_TRIM, файловый канал
# при ADAPTER_DEBUG_ENABLE=1 несёт полные строки. Отдельных рубильников нет.)
```

**Трассировка (trace)** — структурированный JSONL-лог для каждого tool call и ответа:

```bash
# Trace-логи (session-*.jsonl) пишутся в ту же директорию ADAPTER_DEBUG_LOGPATH,
# что и debug-логи; включаются тем же мастер-выключателем файловой записи
# ADAPTER_DEBUG_ENABLE=1. Отдельной переменной пути нет.

# Обрезка полей (0 = без обрезки)
# export ADAPTER_TRACE_REASONING_MAX_CHARS=0
# export ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS=0
```

**Санитайзер** — автоматическое маскирование чувствительных данных (токены, заголовники, ключи):

```bash
# 0 = санитайзер активен (по умолчанию), 1 = отключён (секреты в логах)
# export ADAPTER_SENSITIVE_LOGGING_ENABLE=0
```

Подробнее про логирование — в [`docs/logging.md`](logging.md).

### 5.8 Входные эндпоинты и TARGET-маршрутизация (v0.9.0)

Адаптер принимает **три** POST-эндпоинта: `/v1/messages` (Anthropic Messages),
`/v1/chat/completions` ([OI] Chat Completions) и `/v1/responses` ([OI] Responses).
Что адаптер делает с запросом на каждом входе — решает соответствующая
TARGET-переменная (**префикс имени = входной эндпоинт**); вход, чей целевой
формат `none`, не принимается вовсе (404):

```bash
# /v1/messages → конвертация в chat.completions (ДЕФОЛТ — нулевая настройка,
# прежнее поведение 100%). Прочие значения: messages (passthrough E→E),
# responses, auto (выбор по кэшу проб), none (вход выключен).
export ADAPTER_MESSAGES_TARGET=completions

# /v1/chat/completions: default none — вход закрыт (404).
# completions → passthrough E→E на бэкенд, поддерживающий /v1/chat/completions.
# export ADAPTER_COMPLETIONS_TARGET=completions

# /v1/responses: default none — вход закрыт (404).
# responses → passthrough E→E на бэкенд, поддерживающий /v1/responses.
# export ADAPTER_RESPONSES_TARGET=responses
```

Допустимые значения всех трёх — `completions | messages | responses | auto | none`
(`none` — вход закрыт, 404; `auto` — выбор по кэшу проб, **без сети в запросе**).
Принципы настройки, матрица «вход × значение», реализованные маршруты и
варианты будущих версий — в [`docs/routing.md`](routing.md). Справочник
переменных — [`docs/environment.md`](environment.md), раздел «Входные
эндпоинты (TARGET)»; внутреннее устройство — [`docs/architecture.md`](architecture.md), §4.2.

### 5.9 Полный пример env-файла

Полный рабочий env-файл с комментариями всех переменных — в
`docs/samples/sample.adapter.env` (скопируйте в `adapter.env` и заполните:
путь к YAML в `ADAPTER_BACKEND_CONFIG` и токен бэкенда). Ниже — та же
структура кратко, с пояснениями по блокам:

```bash
# --- Backend connection ---
export ADAPTER_BACKEND_CONFIG="<путь>/adapter.yaml"
export ADAPTER_BACKEND_KEY_LLM_SERVICE="*****"   # имя из поля key YAML-конфига

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
# export ADAPTER_MODELS_MAPPING=":k2-05"

# --- Input endpoint routing (TARGET, v0.9.0) ---
# Дефолты = нулевая настройка: принимается только /v1/messages и конвертируется
# в chat.completions; /v1/chat/completions и /v1/responses закрыты (404).
export ADAPTER_MESSAGES_TARGET=completions
# export ADAPTER_COMPLETIONS_TARGET=completions   # passthrough E→E (вход закрыт при none)
# export ADAPTER_RESPONSES_TARGET=responses       # passthrough E→E (вход закрыт при none)
# export ADAPTER_COMPLETIONS_TARGET=auto          # выбор маршрута по кэшу проб (без сети)

# --- Logging ---
export ADAPTER_DEBUG_ENABLE=0   # файловая запись логов на диск (0 — дефолт: только консоль)
# export ADAPTER_DEBUG_LOGPATH="./tmp/logs"   # директория логов и корень WEBUI (дефолт ./tmp/logs)
# export ADAPTER_DEBUG_PARTS=1    # per-session дампы .json+.yaml всех логгируемых частей
# (ADAPTER_DEBUG_TRIM=3000 — лимит консольных строк; 0 — без обрезки; файл всегда полный)

# --- Sanitizer (secret masking in logs) ---
export ADAPTER_SENSITIVE_LOGGING_ENABLE=0

# --- WEBUI (поднимается всегда; флага отключения нет) ---
# export ADAPTER_WEBUI_PORT=8765
# export ADAPTER_WEBUI_HOST="127.0.0.1"
# export ADAPTER_EXPORTER_ENABLE=1          # Prometheus-экспортёр (отдельный слушатель)
# export ADAPTER_EXPORTER_PORT=9100

# --- Mode ---
export ADAPTER_DETACH_ENABLE=0
```

Переменные `ANTHROPIC_*`/`CLAUDE_CODE_*` — настройки клиента [CC]
(раздел [5.2](#52-настройка-клиента-cc)), в env-файл адаптера они не входят.

---

## 6. Запуск

### 6.1 В foreground (разработка)

```bash
# Подготовить конфиги (один раз) и загрузить переменные
# cp docs/samples/sample.adapter.env  adapter.env
# cp docs/samples/sample.adapter.yaml adapter.yaml
source adapter.env

# Запустить
python3 backend-adapter.py
```

Ожидаемый вывод:

```
======================================================================
Claude Code Adapter v0.9.2 (...
Listening:  http://127.0.0.1:9999
Logs:       file logging off (ADAPTER_DEBUG_ENABLE=0); console debug always on
Models:     strict validation
Streaming:  enabled (SSE passthrough)

Backends:   1 configured:
  - home: http://127.0.0.1:8002
[WEBUI] http://127.0.0.1:8765/ (root: ./tmp/logs)
[EXPORTER] http://127.0.0.1:9100/metrics
======================================================================
```

Сервер запущен, ждёт подключения Claude Code на порту 9999.

Остановить: `Ctrl+C` (SIGINT).

Завершение по Ctrl-C/SIGTERM — вежливое (контракт v0.8.6, реализация —
`backend_adapter/shutdown.py`, покрыта тестами): останавливаются слушатели,
сохраняется usage-хвост `model-usage.yaml`, печатается `[EXIT] Bye`, код
возврата 0. Первый сигнал (SIGINT **и** SIGTERM) обрабатывает единый хэндлер,
который сразу переключает оба сигнала на немедленный `os._exit(130)` и лишь
затем инициирует завершение — повторный (или задвоенный на PyInstaller-бинаре;
сборка бинарей идёт с `--bootloader-ignore-signals`, v0.9.1) сигнал во время
процедуры умирает тихо, без traceback и `[PYI-7290]`.

### 6.2 В фоне (detach-режим, для параллельной работы)

```bash
# Загрузить env
source adapter.env

# Запустить в фоне
ADAPTER_DETACH_ENABLE=1 python3 backend-adapter.py
```

Detach-режим (double fork UNIX-daemon pattern):

- Родительский процесс завершается немедленно
- stdio/stderr перенаправлены в `/dev/null`
- PID-файл пишется в `ADAPTER_DEBUG_LOGPATH` (v0.9.0): имя — `adapter.pid`
  или `basename(ADAPTER_PIDFILE)`)
- Логи идут в директорию `ADAPTER_DEBUG_LOGPATH`, если задана
  (пусто — файловая запись выключена, только консоль)

Управление:

```bash
cat "$ADAPTER_DEBUG_LOGPATH/adapter.pid"   # прочитать PID
kill $(cat "$ADAPTER_DEBUG_LOGPATH/adapter.pid")   # остановить
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

# Health-check WEBUI (порт 8765): процесс жив
curl -s http://127.0.0.1:8765/healthz
# → 200 {"status": "ok", "version": "...", "uptime": ..., "pid": ...}

# Readiness: 200 — бэкенды настроены и прогреты; 503 — ещё нет
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/ready

# Prometheus-метрики (отдельный слушатель, порт 9100)
curl -s http://127.0.0.1:9100/metrics | head

# Попробовать запрос (дефолт: /v1/messages → chat.completions)
curl -X POST http://localhost:9999/v1/messages \
  -H "Content-Type: application/json" \
  -H "Anthropic-Version: 2023-06-01" \
  -H "x-api-key: dummy" \
  -d '{"model":"qwen3.6-35b-a3b","messages":[{"role":"user","content":"Hi"}]}'

# Новые входы (v0.9.0) — работают только при ненулевом TARGET:
# /v1/chat/completions при ADAPTER_COMPLETIONS_TARGET=completions (passthrough),
# /v1/responses при ADAPTER_RESPONSES_TARGET=responses (passthrough),
# при TARGET=none (дефолт) оба отвечают 404.
curl -X POST http://localhost:9999/v1/chat/completions \
  -H "Content-Type: application/json" \
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
# Консольные debug-логи видны всегда; направить их копию в директорию на диск:
# export ADAPTER_DEBUG_ENABLE=1
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
- Структура YAML — `backend:` со списком записей (пример — `docs/samples/sample.adapter.yaml`)
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

Юнит `backend-adapter.service` (шаблон для запуска из исходников — в
`docs/samples/`) рассчитан на установку исходников в `~/backend-adapter`
и подхватывает переменные из env-файла (`EnvironmentFile`).

**Минимальный набор env-переменных** для systemd-юнита:

| Переменная | Значение | Зачем |
|---|---|---|
| `ADAPTER_BACKEND_CONFIG` | путь к YAML | Подключение к бэкенду (пример — `docs/samples/sample.adapter.yaml`) |
| `ADAPTER_PROXY_PORT` | `9999` | Порт, на котором слушает адаптер |
| `ADAPTER_ENDPOINT_HOST` | `127.0.0.1` | Адрес, на котором слушает адаптер (только локально; `0.0.0.0` — все интерфейсы) |
| `ADAPTER_DEBUG_ENABLE` | `0` | Файловая запись логов (консоль — всегда; `1` — писать debug/trace на диск в `ADAPTER_DEBUG_LOGPATH`) |
| `ADAPTER_DETACH_ENABLE` | `0` | **Важно:** не включаем detach при systemd |

> **Важно:** `ADAPTER_DETACH_ENABLE=0` при работе через systemd — systemd сам следит за процессом.
> Включать detach-режим (`ADAPTER_DETACH_ENABLE=1`) вместе с systemd нельзя:
> двойной форк отделит процесс от управления systemd, и `Restart=on-failure` не сработает.

**Установка:**

```bash
# 1. Скопировать юнит в директорию user-юнитов
cp docs/samples/backend-adapter.service ~/.config/systemd/user/backend-adapter.service

# 2. Создать env-файл сервиса (путь из EnvironmentFile юнита — см. шаблон:
#    ~/backend-adapter/backend-adapter.env) и заполнить его
# Примерный набор переменных (полный — в docs/samples/sample.adapter.env):
# ADAPTER_BACKEND_CONFIG="/home/username/backend-adapter/adapter.yaml"
# ADAPTER_PROXY_PORT=9999
# ADAPTER_DEBUG_ENABLE=0
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

На macOS вместо systemd используется **launchd**. Системный файл —
`com.user.backend-adapter.plist` (шаблон для запуска из исходников — в
`docs/samples/`).

**Минимальный набор env-переменных** в `<key>EnvironmentVariables</key>`:

| Переменная | Значение |
|---|---|
| `ADAPTER_BACKEND_CONFIG` | путь к YAML (пример — `docs/samples/sample.adapter.yaml`) |
| `ADAPTER_PROXY_PORT` | `9999` |
| `ADAPTER_ENDPOINT_HOST` | `127.0.0.1` |
| `ADAPTER_DEBUG_ENABLE` | `0` |
| `ADAPTER_DETACH_ENABLE` | `0` |

**Установка:**

```bash
# 1. Скопировать plist в директорию user-задач
cp docs/samples/com.user.backend-adapter.plist \
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
