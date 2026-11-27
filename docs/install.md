# Установка — backend-adapter

> **backend-adapter** (v1.0.3) — HTTP-прокси-адаптер, позволяющий работать агентам с
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

Руководства по настройке клиентов: [docs/claude_code.md](claude_code.md) ([CC]),
[docs/qwen-code.md](qwen-code.md) (QwenCode) и [docs/codex.md](codex.md) (Codex CLI);
входные эндпоинты и маршрутизация — [docs/routing.md](routing.md).

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
├── backend_adapter/            # Доменный пакет (38 модулей, включая __init__.py; artifact_tree* — 8 модулей)
│   ├── config.py              # Парсинг env, конфиг бэкендов (YAML), модели
│   ├── server.py              # HTTP-сервер, Handler, три входа + TARGET-маршрутизация
│   ├── routing.py             # Входные эндпоинты/TARGET: decide() по TARGET
│   ├── convert.py             # Anthropic ↔ [OI] конвертация
│   ├── streaming.py           # SSE streaming passthrough
│   ├── tracer.py              # JSONL trace-логирование, tool-use causality
│   ├── session_log.py         # Per-session логи с FIFO eviction + .err-канал
│   ├── session_registry.py    # Таблица сессий (страница /sessions): строка-кортеж
│   ├── session_settings.py    # Пер-сессионные переопределения Log/TARGET
│   ├── daemon.py              # Detach (double fork)
│   ├── shutdown.py            # Обработка Ctrl-C/SIGTERM
│   ├── logger.py              # Debug-логирование с redaction
│   ├── redact.py              # Маскирование секретов (токены, ключи)
│   ├── webserver.py           # WEBUI-ядро: общий веб-сервер, роутинг эндпойнтов, CLI
│   ├── webui_status.py        # WEBUI-эндпойнт "/": статус (версия, LLM, модели)
│   ├── webui_sessions.py      # WEBUI "/sessions": таблица сессий + /api/sessions/*
│   ├── webui_errors.py        # WEBUI "/errors/<имя>": превью .err-файла сессии
│   ├── webui_ops.py           # WEBUI health-эндпоинты "/healthz" "/health" "/live" "/ready"
│   ├── webui_config_api.py    # WEBUI-эндпойнт "/config": runtime-пул debug-переменных
│   ├── prometheus_exporter.py # Prometheus-метрики /metrics (отдельный слушатель, stdlib-only)
│   ├── session_viewer.py      # WEBUI-эндпойнт "/session": просмотр *.parts сессий
│   ├── model_usage.py         # Персистентный учёт использованных моделей, тарифы
│   ├── probe_json.py          # JSON-результат опроса списка моделей в var/
│   ├── cli.py                 # Консольный entry point пакета
│   ├── cli_args.py            # Разбор argv адаптера: --version/--help/--root/--install
│   ├── templates.py           # Встроенные шаблоны дефолтов для --install (v0.9.12)
│   ├── artifact_tree.py       # artifact_tree*: публичный API (generate())
│   ├── artifact_tree_common.py    # утилиты, константы, цвета
│   ├── artifact_tree_registry.py  # реестр артефактов + дедупликация
│   ├── artifact_tree_parse.py     # разбор дампов, классификация kind
│   ├── artifact_tree_turnbuilder.py  # связывание openai_body/fetch_raw в ходы
│   ├── artifact_tree_plantuml.py  # PlantUML-рендер
│   ├── artifact_tree_graphviz.py  # PNG через plantuml/graphviz-fallback
│   ├── artifact_tree_html.py      # интерактивный tree.html
│   ├── artifact_refresh.py    # фоновая дебаунсная отрисовка артефактов (v0.9.10)
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
│   ├── codex.md                 # Настройка клиента Codex CLI
│   ├── samples/                 # Примеры конфигов (см. раздел 3):
│   │   ├── sample.adapter.env   #   Полный пример env (все переменные адаптера)
│   │   ├── sample.adapter.yaml  #   Пример YAML-конфига бэкендов
│   │   ├── sample.tariffs.yaml  #   Пример тарифов моделей (--install копирует в дом)
│   │   ├── backend-adapter.service      #   системный systemd unit (root, install.sh --service)
│   │   ├── backend-adapter.user.service #   user-level systemd unit (--systemd, без root)
│   │   └── com.user.backend-adapter.plist  # launchd (macOS, из исходников)
│   ├── claude_code/             # Локальные настройки клиента [CC] (не для продакшена)
│   ├── qwen-code/               # Локальные настройки клиента QwenCode (не для продакшена)
│   └── codex/                   # Локальные настройки клиента Codex CLI (не для продакшена)
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
  `ADAPTER_DATA_ROOT`, `ADAPTER_DETACH_ENABLE=0`), права `0600`,
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

**macOS (`--service`).** Установщик размечает дом и делегирует
`backend-adapter --launchd plist-install --root <root>`: ставится launchd-агент
текущего пользователя (`~/Library/LaunchAgents`) с `--root`-подходом —
настройки остаются в `<root>/adapter.env`, токены в plist не попадают.
Плист сам задаёт `ADAPTER_BACKEND_CONFIG=<root>/adapter.yaml` и
`ADAPTER_DETACH_ENABLE=0` (v1.0.3). В отличие от Linux, агент **не
запускается** сразу: заполните токен в `adapter.env` и выполните
`--launchd load`. Системные шаблоны репозитория
(`docs/samples/backend-adapter.service`,
`docs/samples/com.user.backend-adapter.plist`) рассчитаны на запуск из
исходников; для бинарника артефакт генерируется установщиком.

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
| `--service` | Дополнительно установить сервис автозапуска для бинарника. **Linux:** системный systemd-юнит с сервисным пользователем и готовым конфигом в `/var/lib/backend-adapter`, включается и **запускается сразу** (нужен root/`sudo`). **macOS:** launchd-агент пользователя по `--root`-подходу (не запускается сразу — заполните токен в `adapter.env`, затем `--launchd load`) |
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
файловая запись выключена (`ADAPTER_DEBUG_ENABLE=0`), статус-страница
WEBUI доступна на `http://127.0.0.1:8765/`. Руководство по страницам и
JSON-эндпоинтам (включая таблицу использованных моделей и файл
`model-usage.yaml`) — `docs/webui.md`.

Проверить версию и получить список ключей можно без запуска адаптера:
`backend-adapter --version` и `backend-adapter --help` (см.
[4.5](#45-cli-и-домашняя-папка-root-install)).

Когда стоит предпочесть исходники (`git clone` + `pip install -r
requirements.txt` или `./scripts/dev-run.sh`): бинарник собирается под конкретную
ОС/архитектуру и не подходит, если нужен нестандартный Python, свои правки
кода или запуск на платформе вне таблицы выше.

Вместо россыпи `export`-переменных можно завести домашнюю папку и запускать
`backend-adapter --root ~/.ba` — тогда конфиги и данные берутся из неё (см.
[4.5](#45-cli-и-домашняя-папка-root-install)).

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

### 4.5 CLI и домашняя папка (`--root`, `--install`, `--check`)

Начиная с v0.9.11 у адаптера есть командный интерфейс; ключи `--name/--base/
--key` и режим `--check` добавлены в v0.9.13:

| Ключ | Действие |
|---|---|
| `-v`, `--version` | Напечатать версию и завершить работу (код 0). Работает при любом, даже заведомо неверном окружении. |
| `-h`, `--help`, `-?` | Напечатать справку со списком ключей и завершить работу (код 0). |
| `--root <путь>` | Домашняя папка адаптера. Без неё дом ищется сам: текущая папка, затем `~/.ba`. Из дома подставляются недостающие настройки, и он же — корень данных. |
| `--install` | Разметить домашнюю папку (конфиги + каталоги данных) и завершить работу. Адаптер не запускает. |
| `--install --name <имя> --base <url> --key <имя_переменной>` | Разметить дом сразу с одним бэкендом: `--name` — имя бэкенда, `--base` — базовый URL, `--key` — **имя** переменной окружения с токеном. Все три ключа — только вместе и только с `--install`. |
| `--check [--root <путь>]` | Офлайн-проверка конфигов дома (`adapter.env`, `adapter.yaml`, `tariffs.yaml`) и живого окружения: типы значений и устаревшие переменные. Адаптер не запускает. |

Без ключей адаптер стартует как обычно; дом подключается автоматически, если
в текущей папке или в `~/.ba` есть `adapter.env`/`adapter.yaml` (иначе нужен
`ADAPTER_BACKEND_CONFIG` в окружении).

**Поиск дома без `--root` (v0.9.13).** Дом ищется в порядке: (1) явный
`--root`; (2) **текущая папка** — если в ней есть `adapter.env` или
`adapter.yaml`; (3) **`~/.ba`** — по тому же признаку. Первый найденный
каталог становится домом целиком (`ADAPTER_DATA_ROOT`, `adapter.yaml`,
`tariffs.yaml`, переменные `adapter.env`). Если дома нет нигде, поиск ничего
не подключает — работает «нулевой запуск» на `ADAPTER_BACKEND_CONFIG` из
окружения. `--install` и `--check` без `--root` автопоиск не используют: их
цель всегда `~/.ba`.

**Что такое домашняя папка.** Каталог, из которого адаптер берёт настройки,
если их нет в окружении. С v0.9.13 дом **совпадает** с корнем данных
(`ADAPTER_DATA_ROOT`): конфиги и данные (`log/`, `var/`) лежат в одной папке,
подпапка `tmp/adapter` убрана.

```
~/.ba/                # --root И ADAPTER_DATA_ROOT
├── adapter.env       # переменные окружения (рендерит --install)
├── adapter.yaml      # конфигурация бэкендов (встроенный sample.adapter.yaml)
├── tariffs.yaml      # тарифы моделей (встроенный sample.tariffs.yaml)
├── log/              # сессионные логи, *.parts-дампы, .err-инциденты
└── var/              # состояние: adapter.pid, model-usage.yaml, state.yaml
```

**Приоритет — всегда за явным окружением.** Переменные из `adapter.env`
применяются только к **незаданным** переменным; значение, экспортированное в
оболочке, побеждает и файл, и подстановку по умолчанию. Так
`ADAPTER_BACKEND_CONFIG=... python backend-adapter.py --root ~/.ba` продолжит
работать с указанным в оболочке конфигом.

**Разметка дома (одноразово):**

```bash
# Создать ~/.ba со всеми конфигами и каталогами данных
backend-adapter --install

# Или в произвольном каталоге
backend-adapter --install --root /opt/ba

# Или сразу с одним бэкендом (v0.9.13): сгенерировать adapter.yaml
# (name/base/key) и подставить имя переменной токена в adapter.env
backend-adapter --install --root /opt/ba \
    --name demo --base https://llm.example.com --key ADAPTER_DEMO_KEY

# Готовая папка: вписать токен бэкенда в adapter.env и проверить конфиги
backend-adapter --check --root /opt/ba

# Запуск с настройками из дома
backend-adapter --root /opt/ba

# Дом можно не указывать: из /opt/ba достаточно запустить без --root —
# текущая папка опознаётся по наличию adapter.env/adapter.yaml
cd /opt/ba && backend-adapter
```

`--install` печатает `[OK]` по каждому созданному файлу. Если файл уже есть,
он **не теряется**: прежняя копия кладётся рядом как `<файл>.bak` (права
сохраняются — копия `adapter.env` с токеном тоже `0600`), затем файл
перезаписывается свежим из шаблона; о бэкапе сообщает `[WARN]`. Сбой
копирования — `[WARN]` и файл **не** перезаписывается (оригинал важнее). В
`adapter.env` токен — заглушка `*****` (файл создаётся с правами `0600`, так
как в него попадает секрет). Без `--name/--base/--key` `adapter.yaml` —
встроенный образец; с ними — компактная запись одного бэкенда (`backend:` /
`name` / `base` / `key`).

**Режим `--check` (v0.9.13).** Офлайн-диагностика: читает `adapter.env`,
`adapter.yaml` и `tariffs.yaml` в доме (или `~/.ba`) **и** живое окружение,
печатает строки `[OK]`/`[WARN]`/`[ERROR]` и завершается **кодом 1**, если
найдены ошибки типов (нечисло в числовом поле, невалидный bool), иначе `0`.
`[WARN]` сами по себе итог не роняют. Проверяются:

- **типы** всех переменных `ADAPTER_*` по той же таблице, что и при старте
  (`env_validate._ENV_SPECS`);
- **устаревшие/неиспользуемые** имена (`ADAPTER_DEBUG_LOGPATH`,
  `ADAPTER_DEBUG_PARTS`, `ADAPTER_DEBUG_TRIM`, `ADAPTER_DEBUG_BODY_FULL`,
  `ADAPTER_DEBUG_OPENAI_BODY_FULL`, `ADAPTER_BACKEND_BASE`,
  `ADAPTER_BACKEND_KEY`) — `[WARN]` с версией, в которой переменная снята;
- **YAML**: `backend` — список записей с непустыми `name`/`base`/`key`;
  `tariffs` — необязателен.

Режим запускается **до** `validate_env` и импорта `config`, поэтому работает
даже при заведомо битом окружении (как `--version`).

**Шаблоны встроены в код** (v0.9.12): `adapter.yaml`, `tariffs.yaml` и
`adapter.env` берутся из модуля `backend_adapter/templates.py`, а не из
`docs/samples/`. Поэтому `--install` работает одинаково из исходников, из
wheel (`pip install .`) и из standalone-бинарника. Файлы `docs/samples/*.yaml`
в репозитории остаются образцами-справочниками — их содержимое синхронно со
встроенным (это сторожит `tests/test_templates.py`).

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

# Корень данных: корень WEBUI + две подпапки с фиксированными ролями
# (создаются при старте). log/ — debug-логи (session-*.log), trace-логи
# (session-*.jsonl), *.parts дампы и файлы инцидентов session-*.err;
# var/ — adapter.pid, model-usage.yaml, state.yaml, <бэкенд>.models.json.
# Путь всегда непуст — при незаданной/пустой env дефолт ~/.ba (раскрытый ~),
# и он же — домашняя папка --root (v0.9.13: дом и корень данных совпадают).
# Файлы в log/ пишутся только при ADAPTER_DEBUG_ENABLE=1. Исключение — файлы
# инцидентов session-*.err (v0.9.0) и JSON-дампы опроса списка моделей:
# пишутся БЕЗУСЛОВНО при финальном ответе клиенту 4xx/5xx реального
# прокси-запроса (полные запрос и ошибка, без обрезки по TRIM; redact по
# умолчанию, полные данные при ADAPTER_SENSITIVE_LOGGING_ENABLE=1).
# v0.9.9: старое имя ADAPTER_DEBUG_LOGPATH больше НЕ читается.
# export ADAPTER_DATA_ROOT="/tmp/adapter-data"

# Максимальная длина КОНСОЛЬНЫХ debug-строк (символы; 0 — без обрезки).
# Файловый канал (session-*.log при ADAPTER_DEBUG_ENABLE=1) trim НЕ использует —
# пишет полные строки (v0.8.6-реформа). v0.9.13: имя ADAPTER_DEBUG_TRIM больше
# не читается, дефолт снижен 3000 → 1000.
# export ADAPTER_LOG_TRIM=1000

# Имя PID-файла: кладётся в ADAPTER_DATA_ROOT/var (basename значения;
# дефолт — adapter.pid). Пишется при ЛЮБОМ запуске (v0.9.5) и удаляется
# при штатном завершении — управление процессом без консоли.
# export ADAPTER_PIDFILE="adapter.pid"

# JSON/YAML-дампы per-session ВСЕХ логгируемых частей протокола (BODY,
# TOOL_RESULT, OPENAI_BODY, FETCH_RAW, RESPONSE — .json и .yaml парой)
# собираются ВМЕСТЕ С ЛОГАМИ по ADAPTER_DEBUG_ENABLE=1 (v0.9.10; отдельного
# флага ADAPTER_DEBUG_PARTS больше нет — заданное значение даёт лишь [WARN]).

# Веб-интерфейс: / — статус (версия, LLM-эндпойнты, модели), /session — просмотр сессий.
# Поднимается ВСЕГДА (v0.8.6; флага ADAPTER_WEBUI_ENABLE больше нет) на 127.0.0.1:8765 —
# статус-страница доступна сразу; корень — ADAPTER_DATA_ROOT (дефолт ~/.ba;
# *.parts сессий — в подпапке log/, model-usage.yaml — в подпапке var/).
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
# консоль всегда печатается с обрезкой ADAPTER_LOG_TRIM, файловый канал
# при ADAPTER_DEBUG_ENABLE=1 несёт полные строки. Отдельных рубильников нет.)
```

**Трассировка (trace)** — структурированный JSONL-лог для каждого tool call и ответа:

```bash
# Trace-логи (session-*.jsonl) пишутся в ту же папку ADAPTER_DATA_ROOT/log,
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
# /v1/messages → ПРЕОБРАЗОВАНИЕ в chat.completions (ДЕФОЛТ v1.0.0). Прочие
# значения: messages (преобразование messages→messages — сортировка system),
# passthrough (дословно, без преобразования), responses (не реализовано →
# 400), none (вход выключен).
export ADAPTER_MESSAGES_TARGET=completions

# /v1/chat/completions: default completions — дословная копия E→E на
# /v1/chat/completions бэкенда. passthrough — то же без защиты max_tokens;
# none — вход закрыт (404).
export ADAPTER_COMPLETIONS_TARGET=completions

# /v1/responses: default completions — полная конверсия responses→completions
# на /v1/chat/completions бэкенда (v0.9.7) + /model. Прочие значения:
# passthrough → дословная передача на /v1/responses бэкенда;
# responses → внутренний конвертер responses→responses (store:false) + /model;
# none — вход закрыт (404).
export ADAPTER_RESPONSES_TARGET=completions
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

# --- Input endpoint routing (TARGET, v1.0.0) ---
# Дефолты = нулевая настройка: принимаются все три входа, каждый конвертируется
# в chat.completions (completions→completions — дословная копия E→E).
export ADAPTER_MESSAGES_TARGET=completions
export ADAPTER_COMPLETIONS_TARGET=completions
export ADAPTER_RESPONSES_TARGET=completions
# Альтернативы: passthrough — дословно; none — вход закрыт (404).
# export ADAPTER_COMPLETIONS_TARGET=passthrough
# export ADAPTER_RESPONSES_TARGET=passthrough

# --- Logging ---
export ADAPTER_DEBUG_ENABLE=0   # файловая запись логов на диск (0 — дефолт: только консоль)
# export ADAPTER_DATA_ROOT="~/.ba"   # корень данных (лог-папка log/ + состояние var/) и корень WEBUI;
#                                    # с v0.9.13 совпадает с домашней папкой --root
# (ADAPTER_DEBUG_ENABLE=1 включает и *.parts-дампы — v0.9.10)
# (ADAPTER_LOG_TRIM=1000 — лимит консольных строк; 0 — без обрезки; файл всегда полный)

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
Backend-Adapter v1.0.3
Listening:  http://127.0.0.1:9999
Logs:       file logging off (ADAPTER_DEBUG_ENABLE=0); console debug always on
Models:     strict validation
Streaming:  enabled (SSE passthrough)

Backends:   1 configured:
  - home: http://127.0.0.1:8002  (12 models) [default]
TARGET:     messages=completions  completions=completions  responses=completions
[WEBUI] http://127.0.0.1:8765/ (root: ~/.ba)
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
- PID-файл пишется в `ADAPTER_DATA_ROOT/var`: имя — `adapter.pid`
  или `basename(ADAPTER_PIDFILE)`
- Логи идут в `ADAPTER_DATA_ROOT/log`, если включена файловая запись
  (`ADAPTER_DEBUG_ENABLE=1`; иначе только консоль)

> **PID-файл — не только для detach.** Он пишется при **любом** запуске
> (v0.9.5): путь — `ADAPTER_DATA_ROOT/var/adapter.pid`, в консоль печатается
> строка `[PID] <pid> → <путь>`. Это позволяет управлять процессом без
> консоли — в контейнере или в составе службы. При штатном завершении
> (`SIGINT`/`SIGTERM`) и на обычном выходе (ранний `[FATAL]`, необработанное
> исключение) файл удаляется. При **повторном** сигнале (`os._exit(130)`)
> файл остаётся — следующий запуск его перезапишет.

Управление:

```bash
cat "$ADAPTER_DATA_ROOT/var/adapter.pid"   # прочитать PID
kill $(cat "$ADAPTER_DATA_ROOT/var/adapter.pid")   # остановить (SIGTERM)
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

# Новые входы (v0.9.0) — с v1.0.0 работают из коробки (дефолт completions):
# /v1/chat/completions → дословная копия E→E; /v1/responses → конверсия
# responses→completions. При TARGET=none соответствующий вход отвечает 404.
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
# export ADAPTER_DATA_ROOT="/tmp/adapter-data"

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

### 9.0 Полный цикл: установка → сервис → удаление

Сквозная последовательность для обеих платформ: различаются только команды
(в примерах — Linux/`--systemd`; для macOS замените на `--launchd` и
`load`/`unload` вместо `start`/`stop`). Детали каждого шага и отличия
платформ — §[9.1](#91-linux--systemd) и §[9.2](#92-macos--launchd).

#### Шаг 1. Поставить адаптер и разметить дом

```bash
# Linux: разметить дом (~/.ba) и положить user-level systemd-юнит
backend-adapter --systemd install
# macOS: разметить дом и положить launchd-агент
backend-adapter --launchd install
```

`install` (он же подкоманда по умолчанию) делает две вещи: **размечает дом**
(`<root>/adapter.env` со всеми значениями по умолчанию, `adapter.yaml`,
`tariffs.yaml`; существующие файлы уходят в `.bak`) и **кладёт артефакт
сервиса** (plist или юнит). Сам сервис при этом **не включается и не
запускается**.

Если дом уже размечен, а переразметка не нужна — `unit-install` /
`plist-install`: только артефакт, дом не меняется.

> **В артефакт попадает путь к тому бинарнику, которым выполнена эта
> команда.** Это главное, что нужно помнить про установку сервиса. Сервис
> запускает не «backend-adapter вообще», а конкретный файл, записанный в
> plist (`ProgramArguments`) или юнит (`ExecStart`). Приоритет выбора:
>
> 1. **тот процесс, которым вы сейчас работаете** — PyInstaller-бинарник
>    (`sys.executable`);
> 2. **`python backend-adapter.py`** — в артефакт пишутся интерпретатор
>    (`sys.executable`) и путь к скрипту: launchd/systemd не умеют
>    запускать по shebang;
> 3. установленная **консольная команда** `backend-adapter`;
> 4. `<root>/backend-adapter.py`;
> 5. `backend-adapter` из `PATH`.
>
> Каждый кандидат проверяется **пробой `--help`** в изолированном окружении:
> версия, не знающая `--root` (до v0.9.13), — `[FATAL]` с подсказкой
> обновиться. Выбранный путь и версия печатаются при установке
> (`[OK]   Бинарник: …`).
>
> **Следствия, которые ловят чаще всего:**
>
> - **Обновили бинарник — перегенерируйте артефакт.** Сервис продолжит
>   запускать старый путь (или старую версию по тому же пути), пока вы не
>   выполните `plist-install`/`unit-install` заново и не перезапустите
>   сервис (`unload`+`load` / `daemon-reload`+`restart`).
> - **Ставили из исходников — артефакт привязан к venv и пути скрипта.**
>   Перемещение/пересоздание venv или каталога с `backend-adapter.py`
>   ломает сервис; после переноса перегенерируйте артефакт тем же способом.
> - **`install` через «чужой» бинарник.** Если в `PATH` раньше стоит старая
>   сборка, а `install` вы выполнили ею (`backend-adapter --launchd
>   install`), в сервис попадёт именно она. Проблема ловится пробой
>   (`[FATAL]`/`[OK] Бинарник:`), но проверить итог можно и явно:
>   `backend-adapter --launchd plist` / `--systemd unit` печатают
>   содержимое артефакта с фактическими путями.

#### Шаг 2. Заполнить настройки в доме

Все настройки, включая токен, живут в доме и **в артефакт не попадают**:

```bash
# токен бэкенда (имя переменной из поля key: в adapter.yaml)
#   <root>/adapter.env  — права 0600
# адрес бэкенда — <root>/adapter.yaml
```

Дом можно разметить сразу с бэкендом, не правя файлы руками:

```bash
backend-adapter --install --name main --base https://api.example.com --key ADAPTER_BACKEND_KEY_MAIN
```

Артефакт сервиса задаёт только `--root` и два инварианта:
`ADAPTER_BACKEND_CONFIG=<root>/adapter.yaml` и `ADAPTER_DETACH_ENABLE=0`
(detach-форк несовместим с надзором launchd/systemd). Оба попадают в
окружение раньше `adapter.env`, поэтому **побеждают файл** — правьте
артефакт перегенерацией, а не `adapter.env`, если нужно изменить именно их.

#### Шаг 3. Запустить и включить автозагрузку

```bash
# macOS: зарегистрировать агент и запустить его
backend-adapter --launchd load

# Linux: запустить сервис (сейчас)
backend-adapter --systemd start
# Linux: включить автозагрузку при входе (и запустить сразу, если нужно)
systemctl --user enable --now backend-adapter.service
```

Проверка: `--launchd status` / `--systemd status`. Подкоманды адаптера
`start`/`stop`/`load`/`unload` **не** управляют автозагрузкой — её включает
`systemctl --user enable` (Linux) или `launchctl` (macOS). На Linux, чтобы
сервис переживал выход из сессии, дополнительно нужен
`loginctl enable-linger "$USER"`.

#### Шаг 4. Смотреть логи

```bash
backend-adapter --systemd logs           # journald, последние 20 строк
backend-adapter --systemd logs -n 200 -f # глубина + слежение
backend-adapter --launchd logs           # macOS, ~/Library/Logs/backend-adapter/adapter.log
```

На macOS лог агента живёт в файле и никем не ограничен в росте; сбросить его
— `--launchd logs-clear`, а чтобы ротация шла сама — `--launchd log-rotate`
(подробно — §[9.2](#92-macos--launchd)).

#### Шаг 5. Удалить при необходимости

Удаление — **две разные операции**, не перепутайте:

| Команда | Что удаляет | Дом (`adapter.env`, конфиги, токены) |
|---|---|---|
| `--systemd unit-uninstall` / `--launchd plist-uninstall` | только сервис: остановить, снять регистрацию/отключить, удалить юнит/plist и его `.bak` | **не трогает** |
| `--systemd uninstall` / `--launchd uninstall` | всё, что поставил `install`: сервис **и** дом целиком (на macOS ещё и лог сервиса и конфиг ротации `newsyslog`, если он был установлен; записи journald не трогаются) | **удаляет безвозвратно** |

Полное удаление спрашивает подтверждение; `--yes` (`-y`) пропускает вопрос,
а без tty (CI, pipe) и без `--yes` завершается `[FATAL]`. Отказ, если
`--root` указывает на `/`, домашний каталог или каталог без
`adapter.env`/`adapter.yaml`.

```bash
# снять только сервис, сохранив дом
backend-adapter --systemd unit-uninstall
backend-adapter --launchd plist-uninstall

# полное удаление — одной командой
backend-adapter --systemd uninstall        # спросит подтверждение
backend-adapter --systemd uninstall --yes  # без вопроса
```

Обёртки `scripts/install-systemd.sh` и `scripts/install-launchd.sh`
принимают те же подкоманды (`unit-uninstall`/`plist-uninstall`,
`uninstall [--yes]`) и лишь пробрасывают их в бинарник.

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
| `ADAPTER_DEBUG_ENABLE` | `0` | Файловая запись логов (консоль — всегда; `1` — писать debug/trace на диск в `ADAPTER_DATA_ROOT/log`) |
| `ADAPTER_DETACH_ENABLE` | `0` | **Важно:** не включаем detach при systemd |

> **Важно:** `ADAPTER_DETACH_ENABLE=0` при работе через systemd — systemd сам следит за процессом.
> Включать detach-режим (`ADAPTER_DETACH_ENABLE=1`) вместе с systemd нельзя:
> двойной форк отделит процесс от управления systemd, и `Restart=on-failure` не сработает.

> **PID-файл.** При `Type=simple` systemd знает PID сам, и файл ему не нужен —
> но адаптер всё равно пишет `ADAPTER_DATA_ROOT/var/adapter.pid` (v0.9.5) и
> снимает его при остановке. Если нужен `Type=forking`, укажите в юните
> `PIDFile=<ADAPTER_DATA_ROOT>/var/adapter.pid` — путь совпадает с тем, куда
> пишет адаптер. В контейнере (без systemd) это единственный способ адресовать
> процесс: `kill $(cat "$ADAPTER_DATA_ROOT/var/adapter.pid")`.

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
| PID-файл вручную | systemd сам знает PID (адаптер пишет файл в обоих режимах) |
| Подходит для тестов | **подходит для продакшена** |

#### User-level юнит через `--systemd` (без root) — аналог launchd

Если root не нужен и достаточно сервиса **текущего пользователя**, используйте
`--systemd` — Linux-аналог `--launchd`. Юнит ставится в
`~/.config/systemd/user/backend-adapter.service` (или `$XDG_CONFIG_HOME`), а
токены остаются в `~/.ba/adapter.env` (права 0600) и **в юнит не попадают**.
Это отличается от системного `--service` (`/etc/systemd/system`, отдельный
пользователь, root). Сквозной порядок «установка → сервис → удаление» (общий
для обеих платформ) — §[9.0](#90-полный-цикл-установка--сервис--удаление).

Бинарник для юнита выбирается так же, как для plist (v1.0.3): приоритет у
того, которым выполнена команда, и каждый кандидат проверяется пробой
`--help` — версия без `--root` даёт `[FATAL]`. В `ExecStart` юнита попадает
**конкретный путь** этого бинарника, поэтому после обновления бинарника или
переноса `backend-adapter.py`/venv юнит нужно перегенерировать
(`unit-install`) и перезагрузить (`daemon-reload` + `restart`). Подробности —
§[9.0](#90-полный-цикл-установка--сервис--удаление) и §9.2.

```bash
# Установить юнит (разметит дом, если нужно). Запуск — отдельно.
backend-adapter --systemd install

# Дом уже размечен — поставить только юнит, не трогая adapter.env/adapter.yaml
backend-adapter --systemd unit-install

# Управление
backend-adapter --systemd start      # запустить сервис
backend-adapter --systemd stop       # остановить сервис
backend-adapter --systemd status     # статус + процесс
backend-adapter --systemd unit       # показать содержимое юнита

# Логи сервиса из journald (v1.0.2)
backend-adapter --systemd logs           # последние 20 строк
backend-adapter --systemd logs -n 200    # последние 200 строк
backend-adapter --systemd logs -f        # следить в консоли до Ctrl-C
backend-adapter --systemd logs -n 50 -f  # глубина + слежение

# Удалить ТОЛЬКО сервис: остановить, отключить, убрать юнит; дом не трогает
backend-adapter --systemd unit-uninstall

# Полное удаление (v1.0.3): сервис + дом (токены, конфиги). Спрашивает
# подтверждение; --yes — без вопроса (для неинтерактивных сценариев)
backend-adapter --systemd uninstall
backend-adapter --systemd uninstall --yes
```

**Через install-systemd.sh:**

```bash
scripts/install-systemd.sh install
scripts/install-systemd.sh unit-install
scripts/install-systemd.sh start | stop | status | unit
scripts/install-systemd.sh logs [-n N] [-f]
scripts/install-systemd.sh unit-uninstall
scripts/install-systemd.sh uninstall [--yes]
```

`--systemd logs` читает логи из **journald** (юнит пишет в
`StandardOutput=journal`): `journalctl --user -u backend-adapter.service`.
По умолчанию выводит последние 20 строк; `-n/--lines <N>` меняет глубину,
`-f/--follow` не завершается после вывода, а следит за логом в консоли до
Ctrl-C. Флаги `-n`/`-f` допустимы только с `logs` (`[FATAL]`, иначе exit 2).
Нужен `journalctl` в `PATH` (есть в любой системе с systemd). Строки попадают
в журнал сразу: адаптер включает построчную буферизацию stdout/stderr, так
что `logs -f` работает на живом сервисе (подробнее — раздел macOS выше).

**Две операции удаления (v1.0.3).**

`--systemd unit-uninstall` — удаляет **только сервис**: `systemctl --user
disable --now` (остановить и выключить автозагрузку), затем удаление юнита и
его `.bak`, затем `daemon-reload`. Дом (`adapter.env` с токенами, конфиги) **не
трогается**; повторный вызов — чистый no-op. Без `systemctl` в `PATH` файлы
юнита всё равно удаляются (остановка — `[WARN]`).

`--systemd uninstall` — **полное удаление**: делает всё то же, что
`unit-uninstall`, а затем стирает сам дом вместе с токенами и конфигами — это
обратная операция к `install` целиком. Записи journald не удаляются. Перед
удалением печатается план и запрашивается подтверждение; `--yes`
(`-y`) пропускает вопрос, а без tty (CI, pipe) и без `--yes` команда
завершается `[FATAL]`. Защита от ошибки: отказ, если `--root` указывает на `/`,
домашний каталог или каталог без `adapter.env`/`adapter.yaml`.
`--yes` допустим только с `uninstall`.

Переменные окружения обёртки: `BA_ROOT` (`~/.ba`), `BA_BINARY`
(`/usr/local/bin/backend-adapter`), `BA_SYSTEMD_UNIT` (путь к юниту).

> `--systemd install` **только устанавливает** юнит: не включает автозагрузку
> и не запускает сервис. Для автозапуска при входе:
> `systemctl --user enable backend-adapter.service`. Чтобы сервис переживал
> выход из системы (unattended-режим) — включите linger:
> `loginctl enable-linger "$USER"`.
>
> `--systemd install` размечает дом: существующие `adapter.env`/`adapter.yaml`
> уходят в `.bak` и перезаписываются шаблонами. Для готового дома используйте
> `--systemd unit-install` — он только генерирует и кладёт юнит.

Юнит содержит два `Environment=`: `ADAPTER_DETACH_ENABLE=0` (detach — двойной
форк — несовместим с надзором systemd) и `ADAPTER_BACKEND_CONFIG=<root>/
adapter.yaml` (путь к конфигу не должен зависеть от содержимого
`adapter.env`). Обе переменные попадают в окружение раньше файла дома, а файл
ставит значения через `setdefault` — поэтому **юнит побеждает `adapter.env`**.
Токенов в юните нет.

### 9.2 macOS — launchd

На macOS вместо systemd используется **launchd**. Два варианта установки:
**автоматический** (`--launchd install` или `install-launchd.sh`) и
**ручной** (через `docs/samples/`).

#### Способ 1: автоматический (`--root ~/.ba`) — рекомендуется

После `--install` все настройки живут в `~/.ba/adapter.env`. Plist содержит
путь к бинарнику, `--root ~/.ba` и **два инварианта сервиса** в
`<key>EnvironmentVariables</key>`: `ADAPTER_BACKEND_CONFIG` (путь к
`~/.ba/adapter.yaml`) и `ADAPTER_DETACH_ENABLE=0`. Остальное адаптер
подхватывает из `adapter.env`, `adapter.yaml`, `tariffs.yaml`. Токены
остаются в `~/.ba/adapter.env` (права 0600) и **не попадают в plist**.

> **Значения plist побеждают `adapter.env`.** launchd кладёт
> `EnvironmentVariables` в окружение процесса ещё до того, как адаптер
> прочитает файл дома, а файл ставит значения через `setdefault`. Поэтому
> эти две переменные из `adapter.env` не переопределяются — правьте plist
> (перегенерируйте его) или не полагайтесь на файл в этих двух ключах.
>
> Зачем они в plist: без `ADAPTER_BACKEND_CONFIG` агент падал на
> `[FATAL] ADAPTER_BACKEND_CONFIG is not set`, а при `ADAPTER_DETACH_ENABLE=1`
> в `adapter.env` делал двойной форк и выпадал из-под надзора launchd.

**Какой бинарник попадёт в plist.** В `ProgramArguments` записывается
**конкретный путь** того бинарника, которым выполнена команда (сквозной
разбор и следствия — §[9.0](#90-полный-цикл-установка--сервис--удаление)).
Приоритет — **тот бинарник, которым выполнена команда** (`--launchd install`):
frozen-сборка → запуск `python backend-adapter.py` (в plist пишутся
интерпретатор + скрипт) → консольная команда `backend-adapter` →
`<root>/backend-adapter.py` → `backend-adapter` из `PATH`. Каждый кандидат
проверяется пробой `--help`: бинарник, не знающий `--root` (версии до
v0.9.13), — `[FATAL]` с подсказкой обновиться. Именно этот случай давал
`[CC] Adapter v0.8.0` и `[FATAL] ADAPTER_BACKEND_CONFIG is not set`: в `PATH`
лежал старый бинарник, он молча проигнорировал `--root`. Выбранный путь и
версия печатаются при установке. Обновили бинарник — перегенерируйте plist
(`plist-install`) и перезапустите агент (`unload` + `load`).

**Установка одной командой:**

```bash
# Бинарный (из релиза): размечает дом и ставит plist
~/.local/bin/backend-adapter --launchd install

# Или из исходников (Python)
python backend-adapter.py --launchd install
```

Если дом `~/.ba` **уже размечен** (`--install` выполнен раньше) и его не
хочется переразмечать, поставьте только plist — дом не изменится:

```bash
backend-adapter --launchd plist-install
```

**Через install-launchd.sh:**

```bash
# Установка (разметит дом, создаст plist, установит в ~/Library/LaunchAgents)
scripts/install-launchd.sh install

# Только plist для уже размеченного дома (дом не меняется)
scripts/install-launchd.sh plist-install

# Управление
scripts/install-launchd.sh load      # зарегистрировать и запустить агент
scripts/install-launchd.sh unload    # остановить и снять регистрацию
scripts/install-launchd.sh status    # проверить статус
scripts/install-launchd.sh plist     # показать содержимое plist

# Логи агента (v1.0.2)
scripts/install-launchd.sh logs          # последние 20 строк
scripts/install-launchd.sh logs -n 200   # последние 200 строк
scripts/install-launchd.sh logs -f       # следить в консоли до Ctrl-C

# Удалить ТОЛЬКО агент: остановить, снять регистрацию, убрать plist; дом не трогает
scripts/install-launchd.sh plist-uninstall

# Полное удаление (v1.0.3): агент + дом (токены, конфиги) + лог сервиса;
# спрашивает подтверждение, --yes — без вопроса
scripts/install-launchd.sh uninstall
scripts/install-launchd.sh uninstall --yes
```

**`load` действительно запускает агент.** Плист генерируется с
`RunAtLoad=false` (`install` обязан только устанавливать), поэтому `load`
делает два шага: `launchctl bootstrap gui/<uid> <plist>` (регистрация) и
`launchctl kickstart -p gui/<uid>/<label>` (старт). Если агент уже
зарегистрирован, `bootstrap` пропускается — иначе launchd вернул бы ошибку
«Input/output error». `unload` — это `launchctl bootout` (остановка и снятие
регистрации одним шагом), `status` разбирает `launchctl print` (`state`,
`pid`, `last exit code`), а не `launchctl list`.

**Просмотр логов (`--launchd logs`).** launchd без `StandardOutPath`/
`StandardErrorPath` отправляет вывод сервиса в `/dev/null` — консольных
логов не остаётся вовсе. Поэтому генерируемый plist указывает оба ключа на
`~/Library/Logs/backend-adapter/adapter.log` — **вне** `<root>`: каталог
`<root>/log` принадлежит *работающему* адаптеру (`session-*.log`, `.err`,
`*.parts`), и вывод launchd не должен смешиваться с ним. `--launchd logs`
показывает хвост этого файла через `tail`:

```bash
backend-adapter --launchd logs           # последние 20 строк
backend-adapter --launchd logs -n 200    # последние 200 строк
backend-adapter --launchd logs -f        # следить до Ctrl-C (tail -F)
backend-adapter --launchd logs -n 50 -f  # глубина + слежение
```

По умолчанию — последние 20 строк; `-n/--lines <N>` меняет глубину,
`-f/--follow` не завершается после вывода, а следит за файлом до Ctrl-C
(`tail -F` переживает пересоздание файла). Флаги `-n`/`-f` допустимы только
с `logs` (`[FATAL]`, иначе exit 2).

> **Лог пишется сразу (v1.0.2).** Когда stdout — не терминал (а под launchd
> это файл), Python по умолчанию буферизует его блоками ~8 КБ, и файл
> оставался бы пустым, пока процесс жив. Адаптер сам включает построчную
> буферизацию (`sys.stdout.reconfigure(line_buffering=True)`), поэтому строки
> видны в файле немедленно, а `logs -f` работает на живом сервисе. То же
> касается journald на Linux и `docker logs` — внешний `PYTHONUNBUFFERED`
> не нужен.

**Очистка лога (`--launchd logs-clear`, v1.0.3).** launchd открывает файл
лога один раз при старте и сам его не ротирует, поэтому файл растёт без
ограничений (замерено: ~21 МБ за сутки). Сбросить его одной командой:

```bash
backend-adapter --launchd logs-clear
```

Файл **удаляется** (а не обнуляется: `truncate` под launchd оставляет
NUL-дырку — писатель держит своё смещение, открыв файл без `O_APPEND`).
Если агент запущен, он **перезапускается** (`launchctl kickstart -k`), чтобы
отпустить удалённый inode и заново открыть лог; остановленный или
незарегистрированный агент не трогается. Отсутствие файла — `[WARN]`, не
ошибка.

**Ротация лога (`--launchd log-rotate`, v1.0.3, ADR-041).** Вместо ручной
чистки можно поручить ротацию штатному демону macOS — **newsyslog**
(`com.apple.newsyslog`, раз в час в :30):

```bash
backend-adapter --launchd log-rotate           # поставить конфиг (спросит sudo)
backend-adapter --launchd log-rotate-remove    # снять конфиг (спросит sudo)
```

`log-rotate` кладёт `/etc/newsyslog.d/backend-adapter.conf` политикой
**5 архивов × 1 МБ** (gzip, по размеру, режим `600`). Строка указывает
**pid-файл** (`<ADAPTER_DATA_ROOT>/var/adapter.pid` — путь резолвится из
`adapter.env` так же, как его пишет адаптер) и сигнал **SIGHUP**: newsyslog
переименует файл в архив и пошлёт процессу `SIGHUP`, а адаптер под launchd
сам переоткроет лог (`service_log.install_hup_handler`) и продолжит писать в
свежий файл. Эскалация нужна только на запись в `/etc` (`sudo install`);
сам конфиг перед установкой проверяется **без root** (`newsyslog -r -n`) —
невалидная строка отвергается `[FATAL]` до всякого `sudo`.

> Без живого PID-файла `log-rotate` отказывает (`[FATAL]`): newsyslog не
> смог бы уведомить агент, и тот продолжил бы писать в переименованный архив.
> Сначала `--launchd load`, затем `log-rotate`. Человекочитаемый формат
> строки — `man newsyslog.conf`.

`log-rotate-remove` идемпотентен (отсутствующий конфиг — не ошибка), а
`--launchd uninstall` снимает ротацию заодно с сервисом, если она была
установлена.

> **Старые plist.** Если агент установлен **до v1.0.2**, его plist либо не
> содержит `StandardOutPath`/`StandardErrorPath` (логов нет вовсе), либо
> указывает их на `<root>/log/adapter.log` (лог сервиса смешан с логами
> работающего адаптера). Если агент установлен **до v1.0.3**, в plist нет
> `EnvironmentVariables` — и агент падает на пустом `ADAPTER_BACKEND_CONFIG`
> (если тот не задан в `adapter.env`) либо теряет надзор при
> `ADAPTER_DETACH_ENABLE=1`. В обоих случаях перегенерируйте plist:
> `backend-adapter --launchd plist-install` (дом не трогается), затем
> `--launchd unload` + `--launchd load`.

**Две операции удаления (v1.0.3).**

`--launchd plist-uninstall` — удаляет **только агент**: `launchctl bootout`
(если агент зарегистрирован), затем удаление plist и его `.bak`. Дом
(`adapter.env` с токенами, конфиги) **не трогается**; повторный вызов —
чистый no-op. Это обратная операция к `plist-install`.

`--launchd uninstall` — **полное удаление**: делает всё то же, что
`plist-uninstall`, а затем стирает сам дом вместе с токенами и конфигами и
**лог сервиса** (`~/Library/Logs/backend-adapter/`) — это обратная операция к
`install` целиком. Перед удалением печатается план и запрашивается
подтверждение; `--yes` (`-y`) пропускает вопрос, а без tty (CI, pipe) и без
`--yes` команда завершается `[FATAL]`. Защита от ошибки: отказ, если `--root`
указывает на `/`, домашний каталог или каталог без
`adapter.env`/`adapter.yaml`. `--yes` допустим только с `uninstall`.

> `--launchd install` размечает дом: существующие `adapter.env`/`adapter.yaml`
> уходят в `.bak` и перезаписываются шаблонами. Для готового дома используйте
> `--launchd plist-install` — он только генерирует и кладёт plist.

**Переменные окружения (опционально):**

| Переменная | По умолчанию | Описание |
|---|---|---|
| `BA_ROOT` | `~/.ba` | Домашняя папка адаптера |
| `BA_BINARY` | `~/.local/bin/backend-adapter` | Путь к бинарнику |
| `BA_LAUNCHD_PLIST` | `~/Library/LaunchAgents/...` | Путь к plist-файлу |

Готовый plist устанавливается в
`~/Library/LaunchAgents/com.user.backend-adapter.plist`.

#### Способ 2: ручной (из исходников)

Системный файл —
`com.user.backend-adapter.plist` (шаблон для запуска из исходников — в
`docs/samples/`).

Образец использует тот же `--root` подход: plist запускает бинарник с
`--root /Users/USERNAME/.ba`, а адаптер сам читает `adapter.env`,
`adapter.yaml`, `tariffs.yaml`. Поэтому **токены в plist не нужны** — они
остаются в `adapter.env` с правами 0600. В `<key>EnvironmentVariables</key>`
образец содержит только два инварианта сервиса (v1.0.3):

| Переменная | Значение |
|---|---|
| `ADAPTER_BACKEND_CONFIG` | `/Users/USERNAME/.ba/adapter.yaml` |
| `ADAPTER_DETACH_ENABLE` | `0` |

Если вы сознательно **не** используете дом и задаёте всё в plist, этих двух
мало — добавьте в `EnvironmentVariables` также `ADAPTER_PROXY_PORT` (`9999`),
`ADAPTER_ENDPOINT_HOST` (`127.0.0.1`), `ADAPTER_DEBUG_ENABLE` (`0`) и
переменную токена, на которую ссылается поле `key` в YAML (полный список —
`docs/environment.md`). Помните: значения plist побеждают `adapter.env`.

> **Запуск из исходников.** Для `backend-adapter.py` в `ProgramArguments`
> нужны два элемента — интерпретатор и скрипт
> (`<string>/path/to/python3</string><string>/path/to/backend-adapter.py</string>`),
> затем `--root <root>`: launchd не исполняет shebang так, как это делает
> оболочка. Автоматический вариант (`--launchd install`) подставляет это сам.

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
tail -f ~/Library/Logs/backend-adapter/adapter.log
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
