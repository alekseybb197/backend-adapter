# Установка — backend-adapter

> **backend-adapter** (v0.9.3) — HTTP-прокси-адаптер, позволяющий работать агентам с
> **Anthropic-совместимым API** (**[CC]**, **QwenCode**) через бэкенд LLM, который
> реализует **[OI]-совместимый API** (`/v1/chat/completions`), но некорректно
> обрабатывает протокол Anthropic Messages API.

## Обзор

Адаптер решает четыре проблемы:

1. **System messages**. Бэкенд кластеризует system messages в конец диалога — адаптер собирает их
   в одно сообщение в начале, как требует спецификация [OI].
2. **Format mismatch**. Клиент отправляет запросы в формате Anthropic Messages API,
   а бэкенд ожидает [OI] Chat Completions. Адаптер выполняет двунаправленную конвертацию
   (сообщения, инструменты, tool choice).
3. **Model compatibility**. Позволяет использовать модели Qwen (например `qwen3.6-35b-a3b`)
   через [CC] и QwenCode.
4. **Qwen tool_calls fallback**. Модели Qwen иногда возвращают вызовы инструментов в текстовом
   формате с JSON внутри XML-подобных тегов — адаптер автоматически парсит этот формат.

Схема работы:

```
[CC] / QwenCode  <--Anthropic API-->  adapter (localhost:9999)  <--[OI] API-->  LLM Backend
```

Руководства по настройке клиентов: [docs/claude_code.md](claude_code.md) ([CC]) и
[docs/qwen-code.md](qwen-code.md) (QwenCode); входные эндпоинты и маршрутизация —
[docs/routing.md](routing.md).

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

Оба сценария покрывают и **обновление** (раздел [4.2](#42-установка-одной-строкой-curl--bash)):
`converge` ставит «старую» фикстуру (`backend-adapter-old`, `v0.9.2`), а шаг
`side_effect` повторно запускает установщик с «новой»
(`backend-adapter-new`, `v0.9.3`) и ассертит обнаружение прежней установки
(`Mode: update`), сообщение `Updating v0.9.2 -> v0.9.3`, подмену бинарника и
ровно одну загрузку по тому же URL. В сценарии `service` дополнительно
проверяются шаги сервисного обновления: смена `MainPID`, свежие
timestamped-бэкапы `adapter.yaml`/`adapter.env`, сохранение прежнего адреса и
токена в регенерированных конфигах и активный юнит после перезапуска;
третий прогон с той же версией — успешный no-op (`Already up to date`).

Второй сценарий, **`molecule/service/`**, проверяет `install.sh --service` на
Linux с **настоящим systemd**: контейнер запускается с `systemd` как PID 1
(privileged + host cgroup namespace, `molecule/service/molecule.yml`),
установщик вызывается с `ADAPTER_SERVICE_BACKEND_BASE`/`ADAPTER_SERVICE_BACKEND_KEY`
(диалог в CI невозможен), а `verify.yml` ассертит контракт сервиса —
сервисный пользователь, конфиги в `/var/lib/backend-adapter` (в т.ч. права
`0600` на env-файл с токеном), системный юнит и, главное,
`systemctl is-enabled == enabled` **и** `is-active == active` (реальный
запуск). На том же контейнере шаг `side_effect` (`side_effect.yml`) проверяет
и **удаление**: запускает `install.sh --delete --yes` и ассертит, что юнит,
каталог состояния, бинарник и сервисный пользователь исчезли, повторное
удаление — успешный no-op, а `--service --delete` вместе — ошибка. Запуск:

```bash
PATH="$PWD/tmp/molecule-venv/bin:$PATH" tmp/molecule-venv/bin/molecule test -s service
```

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

В CI эти сценарии гоняет отдельный job `install-molecule`
(`.github/workflows/ci.yml`) — но только когда изменения затрагивают сам
установщик: job включается фильтром `dorny/paths-filter` по путям `install.sh`
и `molecule/**` (сценарии читают корневой `install.sh` —
`molecule/install/prepare.yml`). На правках кода, тестов или документации job
пропускается (`skipped`), чтобы не занимать раннер.

Так же по путям гейтятся и остальные job'ы — каждый запускается только на
правках «своей» части репозитория. Фильтры считает лёгкий job `changes`
(он выполняется всегда), а гейт-джобы ссылаются на его output через
`needs` + `if`:

| Фильтр | Пути | Job'ы |
|---|---|---|
| `install` | `install.sh`, `molecule/**` | `install-molecule` |
| `code` | `backend_adapter/**`, `backend-adapter.py`, `tests/**`, `pyproject.toml`, `pytest.ini`, `requirements*.txt` | `lint-and-typecheck`, `test` |
| `runtime` | `backend_adapter/**`, `backend-adapter.py`, `pyproject.toml`, `requirements*.txt` | `install-smoke-test`, `webui-smoke` |

Фильтр `runtime` — это «то, что попадает в сборку пакета»: `tests/**` и
`pytest.ini` на smoke-проверки не влияют, поэтому в него не входят. А
`webui-smoke` намеренно делит гейт с `install-smoke-test`, а не имеет своего
узкого списка webui-модулей: WEBUI-ядро импортирует почти весь базовый пакет
(`config`, `logger`, `redact`, `session_log`, `model_usage`,
`artifact_tree*`, `probe_json`), и узкий список давал бы ложные пропуски при
правке базового модуля.

Практическое следствие: PR, который правит только документацию, прогоняет
лишь job `changes`; правка только `tests/**` запускает `lint-and-typecheck` и
`test`, но не smoke-джобы. Обратная сторона — job без затронутых путей
получает статус `skipped`, поэтому если он объявлен required в
branch-protection, merge заблокируется; набор обязательных проверок нужно
настраивать осознанно.

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
│   ├── claude_code.md           # Настройка клиента [CC]
│   ├── qwen-code.md             # Настройка клиента QwenCode
│   ├── samples/                 # Примеры конфигов (см. раздел 3):
│   │   ├── sample.adapter.env   #   Полный пример env (все переменные адаптера)
│   │   ├── sample.adapter.yaml  #   Пример YAML-конфига бэкендов
│   │   ├── backend-adapter.service      #   systemd unit (Linux, из исходников)
│   │   └── com.user.backend-adapter.plist  # launchd (macOS, из исходников)
│   ├── claude_code/             # Локальные настройки клиента [CC] (не для продакшена)
│   └── qwen-code/               # Локальные настройки клиента QwenCode (не для продакшена)
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
# Linux: системный юнит systemd (/etc/systemd/system), нужен root;
# macOS: launchd-агент текущего пользователя
curl -fsSL https://raw.githubusercontent.com/alekseybb197/backend-adapter/main/install.sh | bash -s -- --service
```

**Linux (`--service`).** Установщик ставит **системный** systemd-юнит
(`/etc/systemd/system/backend-adapter.service`, `WantedBy=multi-user.target`)
и **сразу запускает** его (`systemctl enable --now`). Сервис работает от
выделенного непривилегированного пользователя `backend-adapter`
(`useradd --system --no-create-home`) с корневым каталогом состояния
**`/var/lib/backend-adapter`**, где установщик генерирует **готовый к работе**
конфиг:

- `/var/lib/backend-adapter/adapter.yaml` — один провайдер `main`
  (`base` — адрес бэкенда, `key: ADAPTER_BACKEND_KEY_MAIN` — *имя* переменной
  с токеном), права `0640`, владелец `backend-adapter`;
- `/var/lib/backend-adapter/adapter.env` — минимально достаточный набор
  (`ADAPTER_BACKEND_CONFIG`, `ADAPTER_BACKEND_KEY_MAIN`, `ADAPTER_PROXY_PORT`,
  `ADAPTER_ENDPOINT_HOST`, `ADAPTER_DEBUG_ENABLE`,
  `ADAPTER_DEBUG_LOGPATH`, `ADAPTER_DETACH_ENABLE=0`), права `0600`,
  владелец `root` (токен; systemd читает `EnvironmentFile` от root).

Адрес бэкенда и токен установщик **запрашивает в диалоге** (URL — обычным
вводом, токен — без эха). При запуске не от root установщик повышает права
через `sudo` (проверьте, что он доступен). Для неинтерактивных прогонов
(CI, скрипты) значения передаются переменными окружения
`ADAPTER_SERVICE_BACKEND_BASE` и `ADAPTER_SERVICE_BACKEND_KEY` — тогда диалог
не вызывается. Каталог состояния и имя сервисного пользователя
переопределяются через `ADAPTER_SERVICE_ROOT` / `ADAPTER_SERVICE_USER`.

Управление сервисом (пути/команды печатает установщик в конце):

```bash
systemctl status backend-adapter
systemctl restart backend-adapter
journalctl -u backend-adapter -f
```

**macOS (`--service`).** Ставится launchd-агент текущего пользователя
(`~/Library/LaunchAgents`); launchd не читает env-файлы, поэтому переменные
вписаны прямо в plist, а рядом лежит env-файл-образец для копирования.
В отличие от Linux, агент **не запускается** сразу: заполните
`ADAPTER_BACKEND_CONFIG` в plist и загрузите его вручную. Системные шаблоны
репозитория (`docs/samples/backend-adapter.service`,
`docs/samples/com.user.backend-adapter.plist`) рассчитаны на запуск из
исходников; для бинарника юнит генерируется установщиком.

Удаление установленного бинарника и сервиса (**только Linux**):

```bash
curl -fsSL https://raw.githubusercontent.com/alekseybb197/backend-adapter/main/install.sh | bash -s -- --delete
```

`--delete` снимает всё, что поставил установщик: останавливает и удаляет
systemd-юнит (`systemctl disable --now`), каталог состояния
`/var/lib/backend-adapter` (в нём `adapter.yaml`, логи и **токен** в
`adapter.env`), бинарник `/usr/local/bin/backend-adapter` и сервисного
пользователя `backend-adapter`. Операция идемпотентна: отсутствующие объекты
пропускаются, повторный запуск — успешный no-op. Требует root (при запуске не
от root используется `sudo`). Для неинтерактивного удаления (CI, скрипты)
добавьте `--yes` или `ADAPTER_DELETE_YES=1`. На macOS режим пока **не
поддерживается** — установщик завершится с явной ошибкой и подсказкой, как
снять launchd-агент вручную (раздел [9.2](#92-macos--launchd)). Ключи
`--delete` и `--service` взаимоисключающие.

**Обновление.** Повторный запуск установщика **автоматически** переходит в
режим обновления — отдельного флага не нужно: обнаружив прежнюю установку
(бинарник, systemd-юнит или каталог состояния), скрипт печатает
`Mode: update` и действует по шагам:

1. **скачивает** бинарник последнего релиза во временный каталог, **не
   трогая** установленный;
2. **сравнивает версии** по баннеру: и скачанный, и установленный бинарник
   запускаются с пустым `ADAPTER_BACKEND_CONFIG` и печатают
   `Backend-Adapter vX.Y.Z` (сравнение покомпонентное, поэтому `0.9.10`
   старше `1.0.0`, но новее `0.9.2`; GitHub API не используется);
3. если скачанная версия **не новее** установленной — сообщает
   `Already up to date (vX.Y.Z)` и завершается успешно, **ничего не меняя**
   (no-op; бинарник, конфиги и сервис не трогаются);
4. если версия новее — **останавливает** сервис (при `--service` или
   обнаруженном юните);
5. **бэкапит** прежние настройки с отметкой времени:
   `/var/lib/backend-adapter/adapter.yaml.<YYYYmmdd-HHMMSS>.bak`,
   `adapter.env.<…>.bak` (права `0600` — в нём токен) и
   `backend-adapter.service.<…>.bak`;
6. **обновляет бинарник** и **перегенерирует** конфиги/юнит по шаблонам
   новой версии, **сохраняя** прежние адрес бэкенда (`base:`) и токен: они
   восстанавливаются из старых `adapter.yaml`/`adapter.env`, если не заданы
   переменными `ADAPTER_SERVICE_BACKEND_BASE`/`ADAPTER_SERVICE_BACKEND_KEY`;
7. **запускает сервис заново** (`daemon-reload` + `systemctl enable --now`).

При обновлении без сервиса (бинарник-only) выполняются шаги 1–3 и 6. На
macOS обновляется бинарник и (при `--service` или найденном plist)
перегенерируется launchd-агент с бэкапом файлов; агент, как и при первичной
установке, **не запускается** автоматически. Если версию установленного
бинарника определить не удалось (нестандартный файл), установщик
предупреждает и продолжает обновление.

Опции:

| Опция | Действие |
|---|---|
| `--service` | Дополнительно установить сервис автозапуска для бинарника. **Linux:** системный systemd-юнит с сервисным пользователем и готовым конфигом в `/var/lib/backend-adapter`, включается и **запускается сразу** (нужен root/`sudo`). **macOS:** launchd-агент пользователя (не запускается сразу — заполните `ADAPTER_BACKEND_CONFIG`) |
| `--delete` | Удалить установленный бинарник и сервис (Linux): юнит, каталог `/var/lib/backend-adapter`, бинарник и сервисного пользователя. Нужен root; macOS пока не поддерживается; взаимоисключающий с `--service` |
| `--yes`, `-y` | Пропустить подтверждение `--delete` (для скриптов/CI) |
| `--help` | Показать справку |

Каталог установки фиксирован per-platform и **не переопределяется**: Linux →
`/usr/local/bin`, macOS → `~/.local/bin`. Установка — только бинарник
последнего релиза; сценарии `--pip` (из исходников) и `--prefix` (свой
каталог) удалены — для установки из исходников используйте `git clone` +
venv (раздел [3](#3-клонирование)). Включить сервис можно и переменной
окружения `SERVICE_INSTALL=1` (эквивалент `--service`), удаление —
`DELETE_INSTALL=1` (эквивалент `--delete`), пропуск подтверждения —
`ADAPTER_DELETE_YES=1` (эквивалент `--yes`).

> **Windows.** Bash-установщик Windows не поддерживает: он завершится с
> явной ошибкой и ссылкой на будущий PowerShell-установщик (`install.ps1`)
> или WSL. В WSL платформа определяется как Linux.

> **Security note.** Установщик общается только с github.com (официальные
> релизы и файлы этого репозитория). Установка бинарника — в пределах
> текущего пользователя (Linux — `/usr/local/bin`; при отсутствии прав
> `sudo`). Режимы `--service` и `--delete` на Linux работают с **системным**
> юнитом и требуют root: `--service` создаёт сервисного пользователя, каталог
> `/var/lib/backend-adapter` и запускает сервис; `--delete` удаляет их
> безвозвратно (включая каталог с токеном), поэтому спрашивает подтверждение
> `y/N` (обход — `--yes`). Токен хранится в
> `/var/lib/backend-adapter/adapter.env` с правами `0600` (владелец root).
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
# /v1/messages → ПРЕОБРАЗОВАНИЕ в chat.completions (ДЕФОЛТ — нулевая настройка,
# прежнее поведение 100%). Прочие значения: messages (преобразование
# messages→messages — сортировка system), passthrough (дословно, без
# преобразования), responses (не реализовано → 400), none (вход выключен).
export ADAPTER_MESSAGES_TARGET=completions

# /v1/chat/completions: default none — вход закрыт (404).
# passthrough → дословная передача на /v1/chat/completions бэкенда.
# export ADAPTER_COMPLETIONS_TARGET=passthrough

# /v1/responses: default none — вход закрыт (404).
# passthrough → дословная передача на /v1/responses бэкенда.
# export ADAPTER_RESPONSES_TARGET=passthrough
```

Допустимые значения всех трёх — `messages | completions | responses | passthrough | none`
(конкретный формат — **прямое преобразование** входа в него; `passthrough` —
дословная передача без преобразования; `none` — вход закрыт, 404). Значение
`auto` прежних версий удалено в v0.9.4: в env оно невалидно (консоль `[WARN]`,
вход трактуется как `none`).
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
# export ADAPTER_COMPLETIONS_TARGET=passthrough   # дословно (вход закрыт при none)
# export ADAPTER_RESPONSES_TARGET=passthrough     # дословно (вход закрыт при none)

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
Claude Code Adapter v0.9.3 (...
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
# /v1/chat/completions при ADAPTER_COMPLETIONS_TARGET=passthrough,
# /v1/responses при ADAPTER_RESPONSES_TARGET=passthrough,
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

> **Бинарник из установщика.** Если адаптер установлен через `install.sh`
> (раздел [4.2](#42-установка-одной-строкой-curl--bash)), режим `--service`
> уже ставит **системный** systemd-юнит автоматически: сервисный пользователь
> `backend-adapter`, конфиг в `/var/lib/backend-adapter`, `enable --now`.
> Раздел ниже описывает ручную установку **из исходников** (user-level юнит).

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
