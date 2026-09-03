# Claude Code <-> OpenAI-backend adapter — history / changelog

## v0.7.2 (WIP — накопление в feature/v0.7.2)

### 2026-09-03 Удалён legacy-режим конфигурации бэкенда

**Проблема:** подключение к бэкенду задавалось двумя способами — парой
env-переменных `ADAPTER_BACKEND_BASE`/`ADAPTER_BACKEND_KEY` (legacy) и
YAML-файлом через `ADAPTER_BACKEND_CONFIG`. Двойной путь умножал ветки
в `refresh_models`, `_resolve_backend`, стартовом блоке и webui_status,
а также расходился в документации и env-примерах.

**Решение:** конфигурация бэкенда — только через `ADAPTER_BACKEND_CONFIG`
(структура `backend:`: список записей `name`/`base`/`key`; `key` — имя
env-переменной токена, раскрывается при старте). Legacy-режим полностью
удалён из кода, тестов, документации и env-примеров. Пустой
`ADAPTER_BACKEND_CONFIG` на старте — явный `[FATAL]` с подсказкой
(пример — `sample.adapter.yaml`) и `sys.exit(1)`. Недостижимый финальный
return в `_resolve_backend` заменён на `RuntimeError`.

**Изменения:** backend_adapter/config.py, backend_adapter/webui_status.py,
backend-adapter.py, tests/, backend-adapter.env, sample.adapter.env,
com.user.backend-adapter.plist, README.md, docs/ (architecture, environment,
install, logging, sanitizing), CLAUDE.md, changelog.md; добавлен
sample.adapter.yaml.

### 2026-09-03 CLAUDE.md: в журнал ADR только существенные решения

**Проблема:** в журнале решений `CLAUDE.md` наравне с changelog.md
дублировались записи обо всех изменениях, включая процессные и мелкие
(включение CLAUDE.md в репозиторий, чек-лист проверки перед коммитом) —
хотя полная история изменений уже ведётся в changelog.md, а в журнале,
по замыслу, должны оставаться только решения, затрагивающие архитектуру
и существенный функционал.

**Решение:** зафиксирован принцип отбора: в журнал вносятся только
архитектурные/функциональные решения (новый компонент, контракты
протокола, констрейнты зависимостей, каркас тестирования); мелкие правки
(ссылки на страницах, форматирование, точечные багфиксы без смены
контракта) — только в changelog.md. Две процессные записи (CLAUDE.md в
репозитории, чек-лист перед коммитом) из журнала убраны — их содержание
осталось в changelog.md и в теле самого файла (раздел «Проверка перед
коммитом»); оставшиеся записи упорядочены по датам сверху вниз.

**Изменения:** CLAUDE.md, changelog.md.

### 2026-09-03 CLAUDE.md включён в репозиторий (снят запрет в .gitignore)

**Проблема:** `CLAUDE.md` содержал проектный контекст и обязательный
чек-лист проверки перед коммитом, но был исключён из git (`.gitignore`);
контекст не доезжал до CI и до других участников, а история его правок
(ADR-записи) не сохранялась в репозитории.

**Решение:** запрет `CLAUDE.md` в `.gitignore` снят, файл введён в состав
проекта (теперь `git status`/diff его видят, правки коммитятся вместе с
кодом); шапка файла обновлена («Не коммитится» → «входит в состав
репозитория»).

**Изменения:** .gitignore, CLAUDE.md, changelog.md.

### 2026-09-03 WEBUI /session: ссылка на статус, активная вкладка; чек-лист перед коммитом + строгий mypy

**Проблема:** со страницы просмотра сессий (`/session`) нельзя было одним
кликом вернуться к статус-странице `/` (доступность эндпойнтов, модели);
после обновления страницы активность всегда перескакивала на первую
вкладку, теряя выбранную сессию. Параллельно: обязательная проверка перед
коммитом не была зафиксирована, а дословный `mypy --strict` на пакете
падал (7 ошибок) — заявленный барьер был невыполним.

**Решение:**
- в панели вкладок `/session` слева добавлена ссылка «← статус» на `/`;
  активная вкладка запоминается в `location.hash` (клик пишет hash,
  при загрузке страницы вкладка из hash активируется заново — а не
  первая попавшаяся);
- в `CLAUDE.md` добавлен раздел «Проверка перед коммитом — обязательна»
  с четырьмя командами дословно (ruff check, ruff format --check,
  `mypy --strict --ignore-missing-imports backend_adapter/ backend-adapter.py`,
  pytest);
- 7 strict-ошибок починены без изменения семантики: в `convert.py`
  аннотация `assistant_msg: dict[str, Any]` (иначе mypy сужает тип ключа
  `tool_calls` до `str`); в `server.py` маркер ошибки
  `last_error: tuple[int | str, str] | None` объявлен один раз (стрим-ветка,
  действует на всю функцию — в нестрим-ветке только присваивание);
  в финале нестрим-ветки убран мёртвый код `final_status = code` после
  `elif isinstance(code, int)` (был недостижим и при маркере `"error"`
  вообще не отправлял ответ клиенту) — теперь честно отдаётся 502.

**Изменения:** backend_adapter/session_viewer.py, tests/test_session_viewer.py,
backend_adapter/convert.py, backend_adapter/server.py, CLAUDE.md, changelog.md.

## v0.7.1 (консольный entry point, CI/PR-каркас, единая конфигурация тулинга, 2026-09-02)

### 2026-09-02 v0.7.1: консольный entry point, CI/PR-каркас, единая конфигурация тулинга
**Проблема:** движок CI/PR-разработки отсутствовал: без автоматической
проверки регрессии (ruff/mypy/pytest в CI), без конвенции описания PR
(CONTRIBUTING.md), без установки из PyPI (консольный скрипт не работал:
entry point в pyproject.toml ссылался на несуществующий
`backend_adapter.cli:main`).

**Решение:** добавлен каркас совместной разработки, единая конфигурация
тулинга в pyproject.toml и рабочий entry point:

| Файл | Роль |
|---|---|
| `pyproject.toml` | Пакетная метадата + конфигурация ruff (lint-правила, per-file-ignores), mypy (НЕ-strict: репозиторий исторически не типизирован, конфиг даёт базовую проверку типов без шума от legacy-кода) и pytest. |
| `.github/workflows/ci.yml` | Четыре job: lint-and-typecheck (ruff check/format + mypy), test (pytest+cov, матрица Python 3.10–3.13), install-smoke-test (установка `pip install -e .`, проверка entry point), webui-smoke (standalone `python -m backend_adapter.webserver`, проба `/`, `/session`, 404). |
| `.github/PULL_REQUEST_TEMPLATE.md` | Шаблон описания PR: контекст/проблема, решение, тесты, проверка вручную. |
| `CONTRIBUTING.md` | Конвенции для контрибьюторов: Python 3.10+, venv, ruff/mypy/pytest, ветки `feature/*` от `main`, автор коммита — человек. |
| `backend_adapter/cli.py` | Новый консольный entry point: runpy-трамплин, исполняющий `backend-adapter.py` как `__main__` (имя с дефисом не импортируется, файл не попадает в wheel; версия остаётся в одном месте). |

**Изменения:** pyproject.toml, .github/workflows/ci.yml,
.github/PULL_REQUEST_TEMPLATE.md, CONTRIBUTING.md (новые),
backend_adapter/cli.py (новый), requirements-dev.txt
(добавлены pytest-cov/mypy/ruff — набор инструментов, который гоняет CI),
backend-adapter.py
(версия 0.7.0 → 0.7.1), backend_adapter/* (мелкие правки под ruff:
logger.py — SIM108, server.py — SIM102/SIM105, session_log.py — SIM105,
webserver.py — SIM102/SIM105/UP032, artifact_tree_html.py — F811/N806,
artifact_tree_registry.py — F811, плюс аннотации в artifact_tree_parse.py,
artifact_tree_turnbuilder.py, session_viewer.py), docs/*, README.md,
changelog.md.

### 2026-09-02 WEBUI: общее ядро веб-сервера, эндпойнты /session и / (статус)

**Проблема:** `session_viewer.py` совмещал CLI (main/argparse), создание
сервера, HTTP-обработчик и рендеры в одном растущем файле — добавление
нового эндпойнта требовало правки этого файла, а корень сервера умел
только просмотр сессий (версия кода и состояние LLM-бэкендов нигде не
видны без чтения логов).

**Решение:** веб-часть разделена на общее ядро и модули-эндпойнты.
Новый эндпойнт = новый модуль с `@webserver.register`-классом + одна
строка импорта в `serve()`; роутинг, раздача, 404/405 и контекст
(root_dir/version) достаются автоматически. Корень `/` стал статус-
страницей: версия кода, режим работы, каждый LLM-эндпойнт с
доступностью и списком моделей; кнопка «⟳ Проверить сейчас» выполняет
живую пробу (GET `/v1/models`) с жёстким таймаутом 5 с на эндпойнт.

| Файл | Роль |
|---|---|
| `backend_adapter/webserver.py` | Общее ядро: база `Endpoint` (prefix + GET/POST с дефолтами 404/405), реестр `ENDPOINTS` + декор `register`, единый `Handler`-диспетчер (самый длинный совпавший префикс; корень `/` — только точный путь), `QuietWebServer`, `serve(root_dir, version, ...)`, CLI `python -m backend_adapter.webserver [ROOT] [--port] [--host]`. |
| `backend_adapter/session_viewer.py` | Чистый эндпойнт `/session`: вкладки сессий + раздача файлов. CLI/`serve()`/`Handler` убраны (переехали в ядро). |
| `backend_adapter/webui_status.py` | Новый эндпойнт `/`: статус-страница. GET — snapshot состояния на старте (конфиг-глобалы адаптера, без сети); POST — живая проба всех эндпойнтов. Чистая логика (`_collect_endpoints`/`_config_snapshot`/`_probe_live`) отделена от HTML для тестирования без HTTP. |

**Изменения:** webserver.py, session_viewer.py, webui_status.py,
backend-adapter.py (WEBUI-блок зовёт `webserver.serve(..., __version__, ...)`
— версия передаётся из единственного источника), tests/test_webserver.py,
tests/test_session_viewer.py, tests/test_webui_status.py (новые),
tests/test_artifact_tree.py (smoke-тест импорта под новый API),
docs/architecture.md, docs/environment.md, docs/logging.md, docs/install.md.

ВАЖНО про standalone: `config.BACKEND_BASE` не пуст даже без env
(дефолт `https://llm.service.example.com`) — статус-страница показывает
legacy-эндпойнт только если `ADAPTER_BACKEND_BASE` реально задана в
окружении, иначе честную подсказку «данные адаптера недоступны» вместо
фантомного бэкенда.

Найден и починен баг CLI-запуска ядра: при `python -m backend_adapter.webserver`
runpy исполняет модуль дважды — каноническая копия в `sys.modules` (в неё
регистрируются эндпойнты через `from . import ...`) и namespace `__main__`
с пустым реестром. Если бы `serve()` звался из `__main__`, сервер отвечал
бы 404 на каждый путь; в тестах баг не виден (там `serve()` зовётся из
канонического модуля напрямую). Починено трамплином
`from backend_adapter.webserver import main as _main` в
`if __name__ == "__main__"`; добавлен поведенческий регресс-тест
`test_cli_standalone_serves_builtin_endpoints` (субпроцесс `python -m`,
эндпойнты `/` и `/session` отвечают 200).

### 2026-09-03 WEBUI: обновление списка моделей при загрузке статус-страницы

**Проблема:** кэш моделей (`_AVAILABLE_MODELS`) — snapshot со старта
адаптера. Если бэкенд добавляет модели после старта, строгая валидация
отвергает их (ошибка 400 «model is not available»), а список на странице
статуса обновлялся только кнопкой «⟳ Проверить сейчас».

**Решение:** у кэша моделей появился on-demand refresh без перезапуска
адаптера, и загрузка страницы статуса делает то же, что кнопка.

- `config.py`: новый `refresh_models(timeout=None)` — живая пере-проба
  каждого эндпойнта (`GET /v1/models`) с пересборкой
  `_AVAILABLE_MODELS`/`_MODEL_TO_BACKEND` (общий с init алгоритм коллизий
  вынесен в `_rebuild_index`). Возвращает `{"ok", "count", "errors"}`:
  полный провал/пустой ответ — старый кэш НЕ тронут; частичный успех —
  кэш из ответивших бэкендов, упавшие выпадают. В standalone-режиме
  (viewer вне процесса адаптера) блоки бэкендов перечитываются из
  `ADAPTER_BACKEND_CONFIG`, кнопка работает и без процесса адаптера.
  `_fetch_models` получил параметр `timeout` (None → ADAPTER_TIMEOUT).
  Периодического фонового обновления НЕТ — только по явным сигналам
  (старт адаптера, загрузка страницы, кнопка).
- `webui_status.py`: GET `/` и POST `/` делают одно и то же через общий
  `_refresh_and_render()` — `config.refresh_models(timeout=PROBE_TIMEOUT)`
  (5 с на эндпойнт) и рендер из обновлённых глобалов. При провале
  страница показывает прежний список и текст ошибки. Собственный
  `_probe_live` удалён — его заменил `refresh_models`.

**Изменения:** backend_adapter/config.py, backend_adapter/webui_status.py,
tests/test_config.py (новый `TestRefreshModels`, 11 тестов),
tests/test_webui_status.py (GET `/` обновляет модели с коротким таймаутом),
docs/architecture.md, docs/environment.md, docs/logging.md.

## v0.7.0 (WEBUI: session viewer + artifact tree visualization, merged ADAPTER_DEBUG_TAGS_JSON/YAML)

### Веб-интерфейс просмотра сессий (session_viewer + artifact_tree)

**Проблема:** следить за ходом длинной сессии (что агент отправлял модели,
что модель возвращала — включая reasoning, tool calls и результаты
инструментов) по сырым `.parts`-дампам и JSONL-трейсам вручную
нереально: контент повторяется почти в каждом запросе, а связи
«запрос → ответ → следующий запрос» теряются среди десятков файлов.

**Решение:** в пакет добавлены два модуля (перенесены из `tmp/`), которые
строят из `.parts`-дампов визуальное дерево взаимодействия агента и LLM,
и веб-сервер для просмотра этих деревьев:

| Модуль | Роль |
|---|---|
| `backend_adapter/artifact_tree.py` | Извлекает уникальные текстовые артефакты (system/user-промпты, tool_result, reasoning, toolcall, response — дедупликация по sha256 с учётом волатильных вставок вида `<total_tokens>`) и строит дерево ходов диалога. Генерирует `artefacts/`: артефакты (`.yaml`/`.txt`), `tree.puml`, `tree.png` (PlantUML или Graphviz-fallback), интерактивный `tree.html`. Программный API: `generate(parts_dir, verbose)`. |
| `backend_adapter/session_viewer.py` | Локальный веб-сервер: сканирует корневую папку на `*.parts`-директории, на лету (пере)генерирует `tree.html` (если устарел/отсутствует — сравнение mtime с part-файлами), показывает деревья всех сессий на одной странице во вкладках. URL-схема зеркалит структуру диска 1:1 (`/session/<имя>/...`), поэтому относительные ссылки в `tree.html` на raw-файлы одинаково работают через `file://` и через сервер. |

**Запуск.** Включается флагом `ADAPTER_WEBUI_ENABLE=1`; порт —
`ADAPTER_WEBUI_PORT` (по умолчанию 8765); слушает только `127.0.0.1`.
Веб-сервер стартует **в daemon-потоке внутри процесса адаптера** (а не
отдельным процессом), корень — директория из `ADAPTER_DEBUG_LOGFILE`
(там лежат `*.parts` папки). Срабатывает только если `ADAPTER_DEBUG_LOGFILE`
указывает на директорию; иначе — предупреждение в лог и сервер не стартует.

**Встраивание в пакет:**
- `artifact_tree.py`: убран встроенный `configure_logging()` (`logging.basicConfig`) —
  при импорте из пакета он перехватывал бы корневой логгер и портил формат
  логов адаптера; формат теперь наследуется от адаптера. CLI-запуск
  (`python -m backend_adapter.artifact_tree <parts_dir>`) сохранён через `main()`.
- `session_viewer.py`: импорт `artifact_tree` — относительный (`from . import`);
  добавлена функция `serve(root_dir, host, port, verbose)`, возвращающая инстанс
  `ThreadingHTTPServer` (вызывающий сам решает, когда звать `serve_forever()`);
  ручной CLI-запуск (`python -m backend_adapter.session_viewer [ROOT_DIR]`) сохранён.
- `backend-adapter.py`: перед стартом основного прокси при
  `ADAPTER_WEBUI_ENABLE=1` поднимается `session_viewer` в отдельном
  daemon-потоке.

### Единая переменная для per-session дампов (JSON+YAML парой)

**Проблема:** для per-session дампов существовали две почти одинаковые
переменные — `ADAPTER_DEBUG_TAGS_JSON` (теги для `.json`-файлов) и
`ADAPTER_DEBUG_TAGS_YAML` (теги для дополнительных `.yaml`-файлов).
В реальных конфигах они всегда задавались одним и тем же списком тегов.

**Решение:** переменные объединены в одну — `ADAPTER_DEBUG_TAGS_OUT`,
теперь это **логический флаг**: при `=1` дампы пишутся для всех частей
протокола сразу (фиксированный список
`BODY,OPENAI_BODY,FETCH_RAW,TOOL_RESULT_ERROR,TOOL_RESULT,RESPONSE`),
для каждого тега — парой `.json` + `.yaml`. Срабатывает только если
`ADAPTER_DEBUG_LOGFILE` указывает на директорию (как и WEBUI).
Старые переменные `ADAPTER_DEBUG_TAGS_JSON` / `ADAPTER_DEBUG_TAGS_YAML`
удалены полностью (код, тесты, документация, конфиги).

Изменения:
- `config.py`: вместо двух списков — флаг `ADAPTER_DEBUG_TAGS_OUT` +
  константа `ADAPTER_DEBUG_TAGS_OUT_ALL`; добавлены `ADAPTER_WEBUI_ENABLE`,
  `ADAPTER_WEBUI_PORT`.
- `session_log.py`: проверка в `write_debug_json` — флаг + `_DEBUG_IS_DIR`;
  YAML пишется всегда парой к `.json` (условная ветка убрана).
- `server.py`, `streaming.py`: проверки «тег в списке» → «флаг включён».
- `tests/conftest.py`, `tests/test_session_log.py`: обновлены дефолты env;
  тест «тег вне списка» заменён на `test_flag_off_no_files` и
  `test_no_dir_no_files` (новая семантика: флаг + требование директории).
- Документация: `docs/environment.md`, `docs/logging.md`, `docs/install.md`,
  `docs/architecture.md`, `local-adapter.env`.

## v0.6.9 (YAML section logging alongside JSON)

### YAML-логгирование секций взаимодействия с LLM

**Проблема:** в `.parts`-директориях при `ADAPTER_DEBUG_TAGS_JSON` писались только JSON-файлы — удобный для человека формат (отступы, читаемые строки) отсутствовал. Отладка длинных текстовых блоков (промпты, ответы, reasoning) требовала либо парсить JSON, либо читать сырой трафик.

**Решение:** к уже существующему JSON-логгированию добавлено параллельное YAML-логгирование тех же секций. При `ADAPTER_DEBUG_TAGS_JSON=["OPENAI_BODY", ...]` и `ADAPTER_DEBUG_TAGS_YAML=["OPENAI_BODY", ...]` — для каждого тега пишутся **парные** файлы:

| Файл | Формат | Стиль |
|---|---|---|
| `<session>-NNNN-openai_body.json` | `json.dumps(indent=2)` | машиночитаемый |
| `<session>-NNNN-openai_body.yaml` | `yaml.dump(LiteralDumper)` | человекочитаемый: `|` для многострочных, `"..."` для спецсимволов |

**Новая переменная:** `ADAPTER_DEBUG_TAGS_YAML` — перечисление тегов через запятую, для которых дополнительно пишется `.yaml`. Аналогична `ADAPTER_DEBUG_TAGS_JSON` по формату, но может задавать подмножество тегов (не обязательно полный).

Изменения:
- `config.py`: добавлена `ADAPTER_DEBUG_TAGS_YAML` (строки 64–74).
- `session_log.py`: добавлен `LiteralDumper` (строки 21–72) с патчем на `Emitter.choose_scalar_style` для корректного `|` при `space_break`; `dump_yaml()` (строки 75–82).
- `session_log.py:237-262`: при записи JSON — доп. проверка `ADAPTER_DEBUG_TAGS_YAML`, запись `.yaml` с «сырыми» данными (dict/list/decoded-bytes/parsed-json).
- Зависимость: `PyYAML` (`import yaml` / `from yaml.emitter import Emitter`).

## v0.6.8 (tool_name in TOOL_RESULT — successful)

### Имя инструмента в логе успешных вызовов инструментов

**Проблема:** в `[TOOL_RESULT]` log-строке отсутствовало имя вызванного инструмента — было видно только `tool_use_id`, `parent_req_id`, `is_error=false`, `len`. По строке лога нельзя было сразу понять, какой инструмент вызвался и вернул успешный результат.

**Решение:**

| Было | Теперь |
|---|---|
| `[TOOL_RESULT] tool_use_id=... parent_req_id=... is_error=false len=...` | `[TOOL_RESULT] tool_name=ls tool_use_id=... parent_req_id=... is_error=false len=...` |

Изменения:
- `server.py`: в `[TOOL_RESULT]` `_dr()` и `write_debug_json()` добавлен `tool_name` через `_lookup_tool_use_name()`.

## v0.6.7 (tool_name in TOOL_RESULT_ERROR log)

### Имя инструмента в логе ошибок инструментов

**Проблема:** в `[TOOL_RESULT_ERROR]` log-строке (по умолчанию `ADAPTER_DEBUG_TOOLS_ERROR=1`) отсутствовало имя вызванного инструмента — было видно только `tool_use_id`, `parent_req_id`, `is_error`, `content`. По строке лога нельзя было понять, какой инструмент вызвался и вернул ошибку.

**Решение:**

| Было | Теперь |
|---|---|
| `[TOOL_RESULT_ERROR] {"tool_use_id": "...", "parent_req_id": "...", "is_error": true, "content": "..."}` | `[TOOL_RESULT_ERROR] {"tool_use_id": "...", "tool_name": "...", "parent_req_id": "...", "is_error": true, "content": "..."}` |

Изменения:
- `tracer.py`: `_register_tool_use()` теперь принимает `tool_name`, хранит `tool_use_id → tool_name` в `_tool_use_names`. Добавлен `_lookup_tool_use_name()`.
- `convert.py:252`, `streaming.py:131`: передают `tool_name` в `_register_tool_use()`.
- `server.py`: в `[TOOL_RESULT_ERROR]` snapshot добавлен `tool_name` через `_lookup_tool_use_name()`.

## v0.6.6 (unified BODY_TAGS for parts JSON dump)

### Замена `ADAPTER_DEBUG_OPENAI_BODY_JSON` → `ADAPTER_DEBUG_BODY_TAGS`

**Проблема:** было несколько разрозненных флагов (`ADAPTER_DEBUG_OPENAI_BODY_JSON`, `ADAPTER_DEBUG_BODY_FULL` и т.д.), каждый управлял своей частью. `ADAPTER_DEBUG_OPENAI_BODY_JSON` писал только OpenAI-тела, не позволяя выбирать другие части.

**Решение:** `ADAPTER_DEBUG_BODY_TAGS` — единый список тегов, управляющий **всеми** JSON-parts частями протокола. Теги: `BODY`, `OPENAI_BODY`, `TOOL_RESULT`, `FETCH_RAW`, `RESPONSE` и др. (полный список — в `config.py`).

| Была | Теперь |
|---|---|
| `ADAPTER_DEBUG_OPENAI_BODY_JSON=1` → OpenAI тела в `.parts` | `ADAPTER_DEBUG_BODY_TAGS=["OPENAI_BODY", "BODY", "TOOL_RESULT"]` → выбранные части во всех `.parts` |
| Удалена | |

Формат `.parts` директорий и файлов унифицирован с основным логированием:
- Директория: `session-<YYYYMMDD-HHMMSS>-<sessionID[:8]>.parts`
- Файл: `<sessionID[:8]>-NNNN-<tag>.json` (тег в нижнем регистре)

В `session_log.py`: добавлен `_parts_dir_ts` для timestamp, совместимый с `session_file_ts`. Старые директории `*-jsonparts` устарели.

## v0.6.5 (per-request OpenAI body JSON dump)

### Новая переменная `ADAPTER_DEBUG_OPENAI_BODY_JSON`

**Проблема:** при `ADAPTER_DEBUG_OPENAI_BODY_FULL=1` тело OpenAI-запроса попадает в лог, но обрезано (`ADAPTER_DEBUG_TRIM`), а в `stderr`. Нет возможности выгрузить полный структурированный JSON-файл для каждого запроса.

**Решение:**

| Переменная | Default | Описание |
|---|---|---|
| `ADAPTER_DEBUG_OPENAI_BODY_JSON` | `0` | Запись полных OpenAI-тел запросов в отдельные JSON-файлы. Работает ТОЛЬКО при per-session режиме логов (`ADAPTER_DEBUG=1` + `ADAPTER_DEBUG_LOGFILE` указывает на директорию). В директории логов создаётся `session-<datetime>-<sessid8>.parts/` и туда пишутся `openai-NNNN.json` для каждого POST `/v1/messages` с полным телом запроса. |

Логика:
- Активируется только если `ADAPTER_DEBUG_OPENAI_BODY_JSON=1` И per-session режим логов включён (`_DEBUG_IS_DIR=True`)
- В директории логов создаётся `.parts` поддиректория с тем же session-именем
- Каждый POST `/v1/messages` записывает `openai-NNNN.json` с `json.dump(indent=2)`
- Счётчик защищён `threading.Lock` (сервер multi-threaded)

## v0.6.4 (tool result debug logging)

### Три новые переменные для логгирования результатов инструментов

**Проблема:** `[TOOL_RESULT]` писал только метаданные (id, parent_req_id, is_error, len, content обрезан до 3000 символов). `[TOOL_RESULT_ERROR]` — только для ошибок, тоже обрезан до 3000. Нет возможности включить полный вывод всех результатов без ручного чтения trace JSONL.

**Решение:**

| Переменная | Default | Описание |
|---|---|---|
| `ADAPTER_DEBUG_TOOLS` | `0` | Полные `content` всех результатов инструментов в `[TOOL_RESULT]` лог |
| `ADAPTER_DEBUG_TOOLS_ERROR` | `1` | Детальные ошибки в `[TOOL_RESULT_ERROR]` (по умолчанию включено) |
| `ADAPTER_DEBUG_TOOLS_RESPONSE_FULL` | `0` | Полный `content` без обрезки (игнорирует `ADAPTER_DEBUG_TRIM`) для `ADAPTER_DEBUG_TOOLS` / `ADAPTER_DEBUG_TOOLS_ERROR` |

Логика:
- Старый `[TOOL_RESULT]` (метаданные) пишется всегда (без изменений)
- `ADAPTER_DEBUG_TOOLS=1` → доп. строка `content=...` для всех результатов
- `ADAPTER_DEBUG_TOOLS_ERROR=1` (по умолчание) → доп. строка `[TOOL_RESULT_ERROR] {content: ...}` для ошибок
- `ADAPTER_DEBUG_TOOLS_RESPONSE_FULL=1` → `content` без обрезки (иначе `[:ADAPTER_DEBUG_TRIM]`)

## v0.6.3 (unified response full logging flag)

### Замена `ADAPTER_DEBUG_FETCH_RAW_FULL` → `ADAPTER_DEBUG_RESPONSE_FULL`

**Проблема:** было три разных переключателя полноты (`ADAPTER_DEBUG_BODY_FULL`, `ADAPTER_DEBUG_OPENAI_BODY_FULL`, `ADAPTER_DEBUG_FETCH_RAW_FULL`) и две разных жёстких обрезки `[:800]` / `[:ADAPTER_DEBUG_TRIM]` для `[RESPONSE]` в нестриминговом / стриминговом режимах. Никакая из них не управляла форматированным `[RESPONSE]` — ни нестриминговым (хардкод 800), ни стриминговым.

**Решение:** единая переменная `ADAPTER_DEBUG_RESPONSE_FULL` (0 по умолчанию), управляющая полнотой в двух местах:
- `server.py:[FETCH_RAW]` — сырой ответ бэкенда
- `server.py:[RESPONSE]` — отформатированный Anthropic-ответ (нестриминг)
- `streaming.py:[RESPONSE]` — агрегированный ответ стрима

При `ADAPTER_DEBUG_RESPONSE_FULL=1` — полный вывод без обрезки во всех трёх случаях. Иначе — `[:ADAPTER_DEBUG_TRIM]` (по умолчанию 3000).

## v0.6.2 (SSE response logging + HTTP log req_id)

### Лог агрегированного ответа в стриминговом режиме

**Проблема:** в нестриминговой ветке ответ логируется целиком через `[RESPONSE]` (server.py:485). В стриминговом режиме SSE-чанки шли напрямую в `wfile` без логики — в debug-логе был только `[STREAM_REQUESTED]` и `[FETCH]`, но финального ответа не видно.

**Решение:**
- `streaming.py`: по завершении стрима собирается полный ответ из накопленных буферов (`full_text`, `full_reasoning`, `tool_state`) и пишется один раз через `_dr(..., "[RESPONSE] ...")`
- Формат тот же, что в нестриминговой ветке: `text_len`, `reasoning_len`, `tool_uses_count`, обрезанные `text`/`reasoning`, `tool_uses` со скилл-анализом
- `[RESPONSE]` в стриме — аналог нестримингового `[RESPONSE]` — позволяет сверить что получил клиент, не читая SSE вручную
- В trace: полное событие `response_content` + `usage_report` (input_tokens из stream_options.include_usage)

### `[req_id] [HTTP]` — связка HTTP-логов с SSE-стримом

**Проблема:** `log_message` вызывается из `send_response()` вне контекста `do_POST` — не было доступа к `req_id`, поэтому HTTP-сообщения вида `[HTTP]` не имели идентификатора запроса.

**Решение:**
- `threading.local()` хранит `req_id` / `session_id` на весь `do_POST` — `log_message` читает их и форматирует как `[req_id] [HTTP]`
- try/finally гарантирует cleanup даже при исключениях

## v0.6.1 (sanitizer disable flag + documentation)

### Новая переменная `ADAPTER_SENSITIVE_LOGGING_ENABLE`

**Проблема:** санитайзер (redaction) всегда маскировал секренты в логах. Отладка формата/длины/спецсимволов токенов требовала временного изменения кода `redact.py`.

**Решение:**

- Переменная `ADAPTER_SENSITIVE_LOGGING_ENABLE` (`0` по умолчанию). Значение `1`/`true`/`yes` **полностью отключает** `redact()` на всех точках применения:
  - `logger.py`: `_d()`/`_dr()` записывают строки без `redact()`
  - `tracer.py`: `_trace()` записывает JSONL без `redact.redact()`
  - `server.py`: HTTP-заголовки и ошибки бэкенда записываются без маскировки (3 ветки)
- **Только логи** — сетевой трафик (запросы/ответы к бэкенду) не модифицируется никак
- Обновлена документация: `docs/sanitizing.md` — новая секция §4.1, обновлённая таблица §6, grep-паттерны для двух режимов
- Исправлена китайская вставка в `sanitizing.md:352`

## v0.6.0 (domain package refactoring)

### Разбиение монолита `backend-adapter.py` в пакет `backend_adapter/`

**Проблема:** `backend-adapter.py` содержал ~1780 строк, объединяя логически независимые подсистемы — конфигурацию, конвертацию форматов, стриминг, HTTP-сервер, логирование, трассировку, скиллы. Чтение и навигация по файлу, code review и отладка были затруднены.

**Решение:** монолит разбит на 11 модулей в доменном пакете `backend_adapter/`:

| Модуль | Ответственность |
|---|---|
| `config.py` | Многобэкенд-конфигурация, probe models, SSL, маппинг моделей |
| `convert.py` | Anthropic ↔ OpenAI конвертация сообщений, тулов, tool-choice |
| `daemon.py` | detach в фон, pidfile, timeout/retry при рестарте |
| `logger.py` | `_d`, `_dr` — человеко-читаемый per-session лог |
| `redact.py` | Сведение секретов (токены, ключи) из логов |
| `server.py` | HTTP-сервер (`Adapter`, `QuietThreadingHTTPServer`), `do_POST`, `do_GET` |
| `session_log.py` | FIFO-управление per-session файловыми дескрипторами |
| `skill.py` | Pattern-матчинг скиллов по именам тулов |
| `streaming.py` | SSE passthrough + `stream_openai_to_anthropic()` |
| `tracer.py` | JSONL trace-лог + causality tracking (tool_use → tool_result) |

Архитектура зависимостей (DAG):

```
config → logger, tracer, skill → convert, streaming → server
         ↓          ↓         ↓           ↓           ↓
        redact  (tracer)   (skill)    (convert)  (все выше)
         ↓
     session_log
```

- `backend-adapter.py` теперь —.entry point (~126 строк), который импортирует и запускает пакет.
- mypy clean (zero type errors).
- backward-compatible: все env-переменные и поведение без изменений.

## v0.5.2 (stream usage/input_tokens fix)

### Баг: input_tokens=0 в потоковом режиме

**Проблема:** OpenAI-совместимый бэкенд в SSE-стриминге отдаёт `usage` только при явном `stream_options.include_usage=true`. Адаптер не запрашивал это поле, из-за чего Claude Code всю сессию видел `input_tokens=0` и не мог оценивать заполнение контекстного окна.

**Решение:**

- Новая переменная `ADAPTER_STREAM_INCLUDE_USAGE` (по умолчанию `1`). Без неё адаптер просит бэкенд прислать usage в SSE-чанках через `openai_body["stream_options"] = {"include_usage": True}`.
- Аварийный рубильник `ADAPTER_STREAM_INCLUDE_USAGE=0` — на случай бэкенда, который падает на неизвестном поле `stream_options`.
- `stream_openai_to_anthropic` дополнительно принимает `approx_prompt_chars` — размер сообщений в символах (эвристическая оценка из JSON-сериализации `messages`).
- Если бэкенд вернул usage — пробрасывается реальный `prompt_tokens`.
- Если бэкенд не поддержал `stream_options.include_usage` — используется эвристика `chars / 4`, явно помеченная как оценочная:
  - в debug-логе: `[USAGE_WARN] Backend не вернул usage в стриме — input_tokens оценён эвристически`
  - в trace: новое событие `usage_report` с полями `input_tokens`, `input_tokens_estimated`, `output_tokens`, `streamed`.
- `message_delta` теперь содержит полный `usage` (и `input_tokens`, и `output_tokens`), а не только `output_tokens`.

## v0.5.1 (clean _fetch_models)

### Очистка `_fetch_models` от отладочного вывода

- Удалены `print(f"[FETCH_DEBUG] ...")` и `print(f"[FETCH_ERROR] ...")` из `_fetch_models`.
- Оставлен только `_d(f"[FETCH_ERROR] ...")` в обработчике ошибок — попадает в debug-log при включённой отладке, не засоряет stdout.
- Удалён дубликат legacy-определения `_fetch_models`, который переопределял единственное рабочее определение (было исправлено в предыдущем апдейте).

## v0.5.0 (multi-backend config)

### Множественные бэкенды через YAML-конфиг

**Новая переменная:** `ADAPTER_BACKEND_CONFIG` — путь к YAML-файлу, описывающему несколько бэкендов.
Если переменная не задана — адаптер работает в старом single-backend режиме (`BACKEND_BASE` / `BACKEND_KEY`).

**Формат конфига:**
```yaml
backend:
  - name: AAA
    base: https://llm.service.example.com
    key: ADAPTER_BACKEND_KEY_AAA
  - name: BBB
    base: https://llm.service.another.com
    key: ADAPTER_BACKEND_KEY_BBB
```

| Поле | Описание |
|---|---|
| `name` | Короткое имя бэкенда, используется как префикс для коллизирующих моделей |
| `base` | URL бэкенда (без `/v1/…`) |
| `key` | ИМЯ переменной окружения, содержащей токен доступа |

**Автоматические префиксы моделей:**
- При старте адаптер запрашивает `GET /v1/models` с каждого бэкенда.
- Если модель с одинаковым `id` встречается на нескольких бэкендах — к её имени добавляется префикс `<backend_name>.` (например `AAA.qwen3.6-35b-a3b`).
- Модели без коллизий доступны под своим оригинальным именем.
- `ADAPTER_MODELS_MAPPING` не учитывает префиксы — маппинг составляется независимо.

**Маршрутизация запросов:**
- Явный префикс в имени модели (`AAA.qwen3.6-35b-a3b`) → routing к бэкенду AAA, на бэкенд уходит имя без префикса.
- Префикса нет → lookup в общем списке моделей (`_MODEL_TO_BACKEND`), fallback к первому бэкенду в конфиге.

### Прочее

- Заведён самописный мини-парсер YAML (`_parse_backend_yaml`) — без зависимости от PyYAML.
- Вывод при старте показывает количество конфигурированных бэкендов.

## v0.4.1 (log-filename-fix)

### Исправление именования сессионных логов

**Проблема:** в `_make_session_file()` весь `session_id` подставлялся в безопасное имя файла через `re.sub(r'[^A-Za-z0-9._-]', '_', session_id)`. Длинные session IDs (UUID-подобные) порождали слишком длинные и неуклюжие имена файлов сессий.

**Решение:** обрезка `session_id` до первых 8 символов перед безопасным преобразованием: `re.sub(r'[^A-Za-z0-9._-]', '_', session_id[:8])`.

### Прочее

- Удалён неиспользуемый `import socketserver`.
- Убран хардкод fallback модели `"qwen3.6-35b-a3b"` — теперь при отсутствии `model` в запросе возвращается 400 "Missing required field: model".
- Заведены `__version__` и `__comment__` для единого управления версией и комментариями в заголовке запуска.

## v0.4.0 (streaming SSE passthrough + HTTP/1.0 keep-alive fix)

### Стриминг (SSE passthrough)

**Проблема:** адаптер принудительно шлёл бэкенду `stream: False` и ждал ответ целиком. Пока backend "думал" (иногда десятки секунд), клиент (Claude Code) не видел ни одного байта и рвал соединение по своему таймауту — на выходе `BrokenPipeError` при попытке записать уже готовый ответ.

**Решение:**

- Убран жёсткий `"stream": False`. Клиентский флаг `stream` пробрасывается бэкенду "как есть".
- Новая функция `stream_openai_to_anthropic()` — читает SSE-ответ backend'а построчно (`data: {...}` / `data: [DONE]`) и на лету транслирует каждый чанк в Anthropic streaming-события: `message_start` → чередующиеся `content_block_start/delta/stop` (текст как `text_delta`, tool-calls как `tool_use` + `input_json_delta`, фрагменты `arguments` конкатенируются клиентом по-OpenAI-совски) → `message_delta` → `message_stop`.
- `do_POST` разделён на две ветки:
  - **Потоковая:** retry работает только пока не ушёл ни один байт (этап `urlopen`/заголовков). Если обрыв во время самого стрима — шлём SSE-событие `error` вместо retry (второй `message_start` сломал бы протокол). `BrokenPipeError` во время стрима → `[CLIENT_GONE]` без падения.
  - **Нестриминговая:** без изменений, на случай если бэкенд стриминг не поддерживает.
- Логика ретраев, редактирования секретов, per-session логов и causality (`_register_tool_use`) сохранена и в стриминговом пути.

### Флаг `ADAPTER_STREAMING_ENABLE`

Переключатель "рубильник" в едином стиле с остальными `ADAPTER_*_ENABLE`:

- `ADAPTER_STREAMING_ENABLE=1` (по умолчанию) — новое стримящее поведение: если клиент просит `stream=true`, адаптер честно стримит.
- `ADAPTER_STREAMING_ENABLE=0` (или `false`/`no`) — полный откат к старому поведению: `stream=False` принудительно, даже если клиент просил стриминг. Удобен как аварийный рубильник.
- Режим виден при старте: `Streaming: enabled (SSE passthrough)` / `disabled (legacy stream=False, старое поведение)`.

### HTTP/1.0 keep-alive lie fix

**Проблема:** в `_start_sse()` стоял заголовок `Connection: keep-alive`, хотя `protocol_version` адаптера остаётся дефолтным `"HTTP/1.0"` — при таком protocol_version `http.server` **всегда** закрывает TCP-соединение после ответа. Клиент верил заголовку, клал сокет в пул на переиспользование, а при следующей попытке отправить по нему запрос получал `ECONNRESET` на уже мёртвый сокет — отсюда `"API Error: The operation timed out"` и `"will retry in Xm Ys"` в терминале **уже после** того, как адаптер успешно отдал предыдущий ответ.

**Решение:** `_start_sse()` теперь шлёт `Connection: close` и явно выставляет `self.close_connection = True` — честно информирует клиент, что соединение будет закрыто. Ложные таймауты устранены.

> Примечание: это чинит симптом, но не убирает накладные расходы на TCP+TLS handshake на каждый запрос (агентный цикл шлёт их последовательно). Настоящий keep-alive (переиспользование TCP-соединений) потребует отдельной правки: `protocol_version = "HTTP/1.1"` + chunked-фрейминг.

## v0.3.4 (unbuffered I/O for debug/trace logs)

Отключена буферизация ОС/Python при записи в debug- и trace-логи. До этого `fd.write()` уже вызывал `fd.flush()`, но Python внутри файловых потоков мог держать байты в своём буфере и не сбрасывать их на диск мгновенно — в режиме пер-session логов это приводило к задержкам между моментом события и появлением записи в файле.

**Что изменено:** добавлен явный `fd.flush()` / `f.flush()` после каждого `write()` в:
- `_d()` — дескриптор сессионного debug-файла
- `_trace()` — дескриптор сессионного trace-файла
- `_trace()` — mode «один файл» (через `with open(...)`)

Это гарантирует, что каждое событие попадает в log-file сразу, без задержек на заполнение внутреннего буфера Python.

## v0.3.3 (model probe + validation)

При старте адаптер запрашивает у бэкенда GET `/v1/models` и сохраняет список доступных моделей. После этого:
- `GET /v1/models` — адаптер возвращает этот список в OpenAI-совместимом формате `{object: "list", data: [...]}`.
- `POST /v1/messages` — если модель не входит в список (и `ADAPTER_STRICT_MODELS=1`), возвращается 400 с сообщением о доступных моделях.
- Если бэкенд не доступен или не вернул моделей — адаптер **не стартует** (exit 1), чтобы пользователь сразу увидел проблему.

### Новая конфигурация:

| Переменная | Описание |
|---|---|
| `ADAPTER_STRICT_MODELS` | `1` (по умолчанию) — валидация модели; `0` — пропустить проверку и передать любую модель на бэкенд |

## v0.3.2 (skip log files for unknown sessions)

## v0.3.1 (per-session log files)

### Изменение имени файлов логирования

`ADAPTER_DEBUG_LOGFILE` и `ADAPTER_TRACE_LOGFILE` теперь принимают два формата:

| Значение | Поведение | Файлы |
|---|---|---|
| Полный путь к файлу | Старое поведение — один файл для всех | Как задано |
| Путь к директории | Отдельный файл для каждой сессии | `session-<sessionID>.log` / `session-<sessionID>.jsonl` |

При значении-директории адаптер создаёт файлы вида `session-<YYYYMMDD-HHMMSS>-<sessionID>.log` (debug) и `session-<YYYYMMDD-HHMMSS>-<sessionID>.jsonl` (trace) с автоматическим открытием по режиму append. Timestamp фиксируется при первом обращении к сессии и переиспользуется — весь трафик сессии пишется в один файл. Открываемые дескрипторы ограничены по FIFO (5000 на сессию).

## v0.3.0 (timeout+retry+trace+causality)

### Отличия от v0.2.0:
- **reasoning_content больше НЕ обрезается** в trace-логе (раньше писалось только `reasoning[:500]`). Теперь пишется полная строка в поле `"reasoning"` (с опциональным потолком `ADAPTER_TRACE_REASONING_MAX_CHARS`, по умолчанию без ограничения). Это единственное место, где видно, ПОЧЕМУ модель выбрала тот или иной инструмент/скилл — обрезка убивала именно ту часть рассуждения, где принимается решение.
- **Аргументы tool_call (tool_input)** теперь пишутся в trace целиком (поле `"input"` внутри `tool_uses` события `response_content`), а не только `name`/`skill`. Без них нельзя было отличить содержательно разные вызовы одного и того же инструмента.
- **Добавлена трассировка ИСПОЛНЕНИЯ инструмента**: когда в очередном входящем запросе обнаруживается `tool_result`-блок, эмитится отдельное событие `"tool_result"` с полным содержимым результата и признаком ошибки (`is_error`), а не только сам факт вызова инструмента без исхода.
- **Добавлена явная связь родитель → потомок** между запросами одной сессии: каждый `tool_use`, который модель вернула в ответе на запрос `req_id=P`, регистрируется в session-scoped индексе по его `tool_use_id`. Когда в ПОСЛЕДУЮЩЕМ запросе `req_id=C` приходит `tool_result` с тем же `tool_use_id`, адаптер находит `P` в индексе и пишет событие `"tool_result"` с полями `tool_use_id` и `parent_req_id=P`. Это даёт точную причинно-следственную связь между конкретным вызовом инструмента и запросом, в котором пришёл его результат — надёжно даже при параллельных ветвях (см. `request_kind` ниже), потому что `tool_use_id` уникален для каждого вызова, в отличие от сортировки по времени/msg_count.
- **`request_start` теперь содержит поле `request_kind`** — структурный (не эвристический по содержимому промпта) признак того, к какому "потоку" относится запрос в рамках сессии: `"agent_turn"` (обычный ход агента, `tool_count > 0`), `"structured_output"` (побочный вызов со structured-output схемой в `output_config` — например, генерация заголовка сессии, идёт параллельно основному циклу и НЕ является его веткой) или `"plain"` (ни тулов, ни structured output). Это отделяет параллельные сайдкар-вызовы харнесса от реальных ветвлений основного агентного цикла ещё до какого-либо анализа контента.
- **Событие `"harness_branch"` убрано целиком** — оно смешивало под одним именем два разных по природе явления: собственные инвариант-проверки адаптера (`harness_branch/system_first`, `harness_branch/finish_reason_map`) и реальную деградацию поведения (`harness_branch/text_tool_call_fallback`). Заменено тремя явными событиями:
  - `"adapter_invariant_check"` — только диагностика конвертации Anthropic→OpenAI внутри самого адаптера (сейчас: позиция system-сообщения). Явно НЕ ветвление модели/харнесса.
  - `"tool_call_fallback"` — реальная деградация: backend не отдал нативный `tool_calls`, адаптеру пришлось парсить JSON из текста. Повышает риск некорректного вызова инструмента/скилла.
  - маппинг `finish_reason → stop_reason` больше не отдельное событие, а поля (`finish_reason_raw`/`stop_reason_mapped`) внутри и так существующего `response_content` — это чистая функция одного в другое, отдельное событие под неё было избыточным.
- **Человекочитаемый debug-лог (ADAPTER_DEBUG_LOGFILE) ТОЛЬКО дополнен**, ничего не сокращалось и не удалялось:
  - каждая строка теперь начинается с `[req_id]` (или `[-]` вне контекста конкретного запроса) — раньше при параллельных запросах строки разных `req_id` перемежались без возможности их различить, не сверяясь с trace JSON;
  - добавлена отдельная строка `[TOOL_RESULT]` в момент разбора входящего `tool_result`-блока — раньше результат тула был виден только внутри большого дампа `[BODY]`/`[OPENAI_BODY]`;
  - добавлена строка `[STREAM_REQUESTED]` — фиксирует, просил ли исходный запрос Claude Code потоковую выдачу (`anthropic_req["stream"]`), тогда как адаптер к бэкенду всегда шлёт `stream=False`; раньше это расхождение нигде не логировалось.

Аналитика по-прежнему должна строиться по JSONL trace-логу — debug-лог остаётся вспомогательным для ручной отладки конкретной сессии.

### Новая конфигурация (в дополнение к переменным v0.2.0):

| Переменная | Описание |
|---|---|
| `ADAPTER_TRACE_LOGFILE` | Путь к JSONL trace-логу (если не задан — трассировка событий harness/skill не пишется, но redaction всё равно применяется к debug-логу) |
| `ADAPTER_SKILL_PATTERNS` | Путь к JSON-файлу с описанием паттернов скиллов (см. `skill_patterns.json`). Если не задан — используется встроенный дефолт под текущий CLAUDE.md (devtools, frontmatter, klast, mytasks, prreview; и `.claude`/`.qwen` варианты) |
| `ADAPTER_TRACE_REASONING_MAX_CHARS` | Опциональный потолок длины поля `"reasoning"` в trace (0 или не задано — без ограничения, пишется полностью) |
| `ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS` | Опциональный потолок длины полей `"input"` (аргументы tool_call) и `"content"` (tool_result) в trace (0 или не задано — без ограничения). Предохранитель на случай очень больших результатов (вывод команды на десятки МБ), а не дефолтное поведение |
