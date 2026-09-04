# Logging — Полный список лог-блоков backend-adapter

Все лог-блоки разделены на три механизма:

| Механизм | Куда пишется | Функция |
|---|---|---|
| **Debug blocks** `[...]` | `<LOGPATH>/session-<ts>-<sid>.log` | `_dr(req_id, "[NAME] ...")` |
| **Console-only blocks** | Stdout/stderr | `_d("[NAME] ...")` |
| **Structured traces** | `<LOGPATH>/session-<ts>-<sid>.jsonl` | `_trace(...)` |

Также дополнительные механизмы: **JSON/YAML дампы** per-session через `ADAPTER_DEBUG_TAGS_OUT`.

---

## Направление данных

Поток данных через адаптер:

```
← CLIENT [Anthropic] → adapter конвертация → → BACKEND [OpenAI] → бэкенд → ← BACKEND [OpenAI] → adapter конвертация → → CLIENT [Anthropic]
```

| Метка | Что означает |
|---|---|
| **← CLIENT** | Данные, полученные от клиента (до конвертации в OpenAI) |
| **→ BACKEND** | Данные, отправляемые в LLM backend (после конвертации из Anthropic) |
| **← BACKEND** | Данные, полученные от LLM backend (до конвертации в Anthropic) |
| **→ CLIENT** | Данные, отправляемые клиенту (после конвертации из OpenAI) |
| **INTERNAL** | Мониторинг и отладка — не данные обмена |

---

## Debug blocks `[...]` — `<LOGPATH>/session-*.log`

Все три механизма (debug-логи, trace-логи, `.parts`-дампы) пишутся в
**одну директорию** — `ADAPTER_DEBUG_LOGPATH`; пусто (дефолт) — **файловая
запись выключена** (консольные debug-блоки видны при `ADAPTER_DEBUG_ENABLE=1`,
диск не используется, папка не создаётся). Заданный путь — директория;
создаётся при необходимости. Формат
имени сессионного файла — `session-<YYYYMMDD-HHMMSS>-<session_id>.<ext>`
(`.log` — debug, `.jsonl` — trace). Запись подчинена мастер-выключателю
`ADAPTER_DEBUG_ENABLE`; при `ADAPTER_DEBUG_ENABLE=0` ничего не пишется и
папка не создаётся.

### ← CLIENT — Данные от клиента

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 1 | `[REQ]` | ← CLIENT | всегда | HTTP метод, путь, session_id |
| 2 | `[BODY]` | ← CLIENT | `_trim_limit('BODY')` → `ADAPTER_DEBUG_TAGS_FULL` содержит `"BODY"` | Полное тело Anthropic-запроса клиента. Иначе — усечено до `ADAPTER_DEBUG_TRIM` символов |

### → BACKEND — Подготовка и отправка в бэкенд

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 3 | `[MODEL_MAP]` | INTERNAL | всегда | Агентная модель → mapped бэкенд-модель |
| 4 | `[BACKEND_RESOLVE]` | INTERNAL | всегда | Разрешённое имя бэкенда и модель |
| 5 | `[STREAM_DISABLED]` | INTERNAL | `ADAPTER_STREAMING_ENABLE=0` | Бэкенд принудительно в non-stream |
| 6 | `[STREAM_REQUESTED]` | INTERNAL | всегда | Флаги: client stream → backend stream, режим |
| 7 | `[TOOLS]` | INTERNAL | всегда | Количество инструментов, переданных в бэкенд |
| 8 | `[TOOL_CHOICE]` | INTERNAL | всегда | Конфигурация tool choice |
| 9 | `[CHECK]` | INTERNAL | всегда | Инвариант: "First message is system, OK" |
| 10 | `[WARN]` (invariant) | INTERNAL | всегда | Инвариант нарушен: "First message is NOT system: \<role\>" |
| 11 | `[OPENAI_BODY]` | → BACKEND | `ADAPTER_DEBUG_TAGS_FULL` содержит `"OPENAI_BODY"` | Полное тело OpenAI-запроса, отправляемого в бэкенд. Иначе — усечено до `ADAPTER_DEBUG_TRIM` символов |

---

### ← CLIENT — Tool result обработка

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 12 | `[TOOL_RESULT]` (summary) | ← CLIENT | всегда | `tool_name`, `tool_use_id`, `parent_req_id`, `is_error`, `len(content)` |
| 13 | `[TOOL_RESULT]` (content) | ← CLIENT | `ADAPTER_DEBUG_TOOLS=1` | Полный JSON content tool result. Дополнительно: при `ADAPTER_DEBUG_TAGS_OUT=1` пишутся JSON/YAML-файлы per-session |
| 14 | `[TOOL_RESULT_ERROR]` | ← CLIENT | `ADAPTER_DEBUG_TOOLS_ERROR=1` | Полный JSON ошибки. Дополнительно: при `ADAPTER_DEBUG_TAGS_OUT=1` пишутся JSON/YAML-файлы per-session |

### → BACKEND — FETCH (request phase, отправка запроса)

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 15 | `[FETCH]` | INTERNAL | всегда | Префикс "(stream)" или нет, номер попытки, retry count, таймаут |

### ← BACKEND — Ответ от бэкенда

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 16 | `[FETCH_RAW]` | ← BACKEND | `_trim_limit('FETCH_RAW')` → `ADAPTER_DEBUG_TAGS_FULL` содержит `"FETCH_RAW"` | Сырой ответ бэкенда (OpenAI format), до конвертации. Иначе — усечено до `ADAPTER_DEBUG_TRIM` символов |
| 17 | `[FETCH]` (success) | INTERNAL | всегда | Elapsed time, HTTP status, размер ответа в байтах |

### → CLIENT — Конвертация и отправка ответа

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 18 | `[RESPONSE]` (non-stream) | → CLIENT | `_trim_limit('RESPONSE')` | Полностью преобразованный Anthropic-ответ |
| 19 | `[RESPONSE]` (stream) | → CLIENT | всегда | Агрегированный snapshot стримированного ответа: text, reasoning, tool_uses, длины. Дополнительно: при `ADAPTER_DEBUG_TAGS_OUT=1` пишутся JSON/YAML-файлы per-session |

### → CLIENT — Статус завершения

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 20 | `[OK]` (non-stream) | → CLIENT | всегда | "Done" |
| 21 | `[OK]` (stream) | → CLIENT | всегда | "Stream done, stop_reason=..." |
| 22 | `[ERROR]` | INTERNAL | всегда | Ошибка валидации модели |

### INTERNAL — Ошибки и события

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 23 | `[BACKEND_ERR]` | INTERNAL | всегда | Код и сообщение об ошибке HTTP бэкенда |
| 24 | `[RETRY]` | INTERNAL | всегда | Тайминги повторов попытки |
| 25 | `[TIMEOUT]` | INTERNAL | всегда | Событие таймаута |
| 26 | `[CLIENT_GONE]` (stream) | INTERNAL | всегда | Клиент отключился во время стриминга |
| 27 | `[FETCH_ERR]` | INTERNAL | всегда | Исключение при fetch: тип + сообщение |
| 28 | `[FAIL]` | INTERNAL | всегда | Финальная ошибка: 504/502/error code |
| 29 | `[STREAM_WARN]` | INTERNAL | `stream=True` | SSE chunk parse failure |
| 30 | `[USAGE_WARN]` | INTERNAL | `stream=True` | Бэкенд не вернул usage (input_tokens estimated) |

---

## Console-only blocks — `_d("[...] ...")` (только stdout)

| # | Block | Где | Что |
|---|---|---|---|
| 1 | `[CLIENT_GONE]` | `server.py:57` | Socket-level client gone |
| 2 | `[HTTP]` | `server.py:69` | Generic HTTP handler log |
| 3 | `[CLIENT_GONE]` | `server.py:84` | Client gone during JSON send |
| 4 | `[WARN]` | `server.py:86` | Ошибка отправки ответа клиенту |
| 5 | `[SKILL_PATTERNS]` | `skill.py:33` | Не удалось загрузить файлы паттернов скиллов |
| 6 | `[INIT]` | `backend-adapter.py:108` | Старт адаптера: путь к YAML-конфигу бэкендов |

---

## JSON / YAML дампы per-session — `ADAPTER_DEBUG_TAGS_OUT`

| Переменная | Default | Описание |
|---|---|---|
| `ADAPTER_DEBUG_TAGS_FULL` | `""` | Перечисление тегов через запятую, для которых **отключается** обрезка (trim). Например: `"BODY,TOOL_RESULT,RESPONSE"`. Если тег в списке — `_trim_limit(tag)` возвращает `None`, и данные записываются полностью в `_dr()` и в JSON-дамп. |
| `ADAPTER_DEBUG_TAGS_OUT` | `0` (выключено) | **Флаг**: включить per-session дампы **всех** частей протокола (список фиксирован — см. ниже). Для каждого тега пишется **пара файлов**: `.json` (машиночитаемый, `json.dumps(indent=2)`) и `.yaml` (человекочитаемый, `yaml.dump(LiteralDumper)`). Файлы пишутся функцией `write_debug_json(session_id, tag, data)` из `session_log.py`. Требует `ADAPTER_DEBUG_ENABLE=1` и директорию логов `ADAPTER_DEBUG_LOGPATH` (создаётся при необходимости). |

**Тэги для дампов (фиксированный список):** `BODY`, `TOOL_RESULT`, `TOOL_RESULT_ERROR`, `OPENAI_BODY`, `FETCH_RAW`, `RESPONSE`.

**Поведение:**
- JSON/YAML файлы пишутся в ту же директорию, что и debug/trace-логи (`ADAPTER_DEBUG_LOGPATH`).
- Дампы подчинены `ADAPTER_DEBUG_ENABLE` (мастер-выключатель): при `0` не пишутся.
- JSON-дамп `RESPONSE` при stream-режиме также пишется агрегированный snapshot (см. `streaming.py`).

---

## Structured trace events — `<LOGPATH>/session-*.jsonl`

JSONL-события с полями `ts`, `session_id`, `req_id`, `seq`, `event`.

| # | Event | Направление | Что содержит |
|---|---|---|---|
| 1 | `request_start` | ← CLIENT / INTERNAL | `path`, `model`, `max_tokens`, `msg_count`, `tool_count`, `tool_names`, `tool_choice`, `request_kind`, `stream_requested` |
| 2 | `model_map` | INTERNAL | `agent_model`, `backend_model` |
| 3 | `tool_result` | ← CLIENT | `tool_use_id`, `parent_req_id`, `is_error`, `content` |
| 4 | `adapter_invariant_check` | INTERNAL | `check` ("system_message_first"), `passed`, `first_role` |
| 5 | `backend_attempt` | INTERNAL | `attempt`, `timeout`, `streaming` |
| 6 | `backend_result` | INTERNAL | `attempt`, `ok`, `status`, `error`, `elapsed_ms` |
| 7 | `request_end` | → CLIENT / INTERNAL | `http_status`, `retries_used`, `total_elapsed_ms`, `streamed`, `failed`, `client_gone`, `failed_mid_stream` |
| 8 | `tool_call_fallback` | INTERNAL | `parsed_count`, `raw_text_len` |
| 9 | `skill_signal` | INTERNAL | `tool_id`, `tool_name`, `skill`, `evidence` |
| 10 | `response_content` | → CLIENT | `text_len`, `tool_uses`, `finish_reason_raw`, `stop_reason_mapped`, `reasoning_present`, `reasoning_len`, `reasoning`, `streamed` |
| 11 | `usage_report` | ← BACKEND / INTERNAL | `input_tokens`, `input_tokens_estimated`, `output_tokens`, `streamed` |

---

## Сводная матрица по направлениям

### → BACKEND — данные, отправляемые в LLM backend

| Block | Описание |
|---|---|
| `[OPENAI_BODY]` | Тело OpenAI-формата, отправляемое POST-запросом в бэкенд (converted from Anthropic) |
| `OPENAI_BODY` (JSON/YAML) | То же самое, в отдельном JSON/YAML-файле по сессии (`ADAPTER_DEBUG_TAGS_OUT`) |
| `[TOOLS]` | Мета: количество инструментов (подсказка, не само тело запроса) |
| `[TOOL_CHOICE]` | Мета: конфигурация tool choice |
| `[STREAM_REQUESTED]` | Мета: negotiated streaming flags |

### ← BACKEND — данные, полученные от LLM backend

| Block | Описание |
|---|---|
| `[FETCH_RAW]` | Сырой HTTP-ответ бэкенда (OpenAI format), до конвертации (только non-stream) |
| `[RESPONSE]` (stream) | Агрегированный snapshot стримированного ответа (text, reasoning, tool_uses) |
| `[RESPONSE]` (non-stream) | Полностью преобразованный Anthropic-ответ для клиента |
| `[USAGE_WARN]` | Предупреждение: бэкенд не вернул usage info |

### → CLIENT — данные, отправляемые клиенту

| Block | Описание |
|---|---|
| `[RESPONSE]` (non-stream) | Ответ JSON клиенту (после конвертации из OpenAI) |
| `[RESPONSE]` (stream) | Snapshot стримированного ответа |
| `[OK]` | Статус завершения: "Done" или "Stream done, stop_reason=..." |

### ← CLIENT — данные, полученные от клиента

| Block | Описание |
|---|---|
| `[REQ]` | HTTP metadata: метод, путь, session_id |
| `[BODY]` | Исходное Anthropic-тело запроса клиента |
| `[TOOL_RESULT]` (summary) | `tool_use_id`, `parent_req_id`, `is_error`, `len(content)` (всегда) |
| `[TOOL_RESULT]` (content) | Полный JSON content tool result (`ADAPTER_DEBUG_TOOLS=1`) |
| `[TOOL_RESULT_ERROR]` | Снэпшот ошибки: `tool_use_id`, `parent_req_id`, `is_error`, `content` (`ADAPTER_DEBUG_TOOLS_ERROR=1`, по умолчанию `0` — выкл) |

### INTERNAL — мониторинг и отладка

| Block | Описание |
|---|---|
| `[MODEL_MAP]` | Mapping agent → backend model |
| `[BACKEND_RESOLVE]` | Выбранный бэкенд и модель |
| `[STREAM_DISABLED]` | Форс non-stream |
| `[CHECK]` / `[WARN]` (invariant) | Проверка инварианта adapter (system message first) |
| `[FETCH]` (request, success) | Попробная информация и метрики fetch |
| `[BACKEND_ERR]` | HTTP error бэкенда |
| `[RETRY]` | Тайминги повторов |
| `[TIMEOUT]` | Таймауты |
| `[CLIENT_GONE]` | Отключение клиента |
| `[FETCH_ERR]` | Исключения fetch |
| `[FAIL]` | Финальные ошибки |
| `[STREAM_WARN]` | SSE parse failures |
| `[USAGE_WARN]` | Missing usage from backend |
| `[ERROR]` | Валидация модели |
| Console-only: `[CLIENT_GONE]`, `[HTTP]`, `[WARN]`, `[SKILL_PATTERNS]`, `[INIT]` | Системные логи |
| Все trace events | Структурированное журналирование (см. таблицу выше) |

---

## Просмотр сессий в браузере — `ADAPTER_WEBUI_ENABLE`

Веб-интерфейс (общее ядро `backend_adapter/webserver.py` + эндпойнты)
показывает «треки» общения агента и LLM по `.parts`-дампам: для каждой
сессии строится дерево артефактов (`artefacts/tree.html`), страница
открывается во вкладках.

- Включается флагом `ADAPTER_WEBUI_ENABLE` (**по умолчанию `1`** —
  статус-страница доступна сразу), порт — `ADAPTER_WEBUI_PORT`
  (по умолчанию `8765`), слушает на `ADAPTER_WEBUI_HOST` (по умолчанию
  `127.0.0.1` — только локально; `0.0.0.0` — доступ из сети).
- Адреса: `http://127.0.0.1:<port>/` — статус-страница (версия кода,
  режим работы, LLM-эндпойнты с доступностью и списком моделей);
  `http://127.0.0.1:<port>/session` — вкладки просмотра сессий;
  `http://127.0.0.1:<port>/config` — runtime-переключение объёма
  debug-записи (см. ниже).
- Список моделей на странице статуса обновляется при каждой загрузке
  страницы и по кнопке «⟳ Проверить сейчас» — оба случая делают одно и то
  же: `config.refresh_models()` (живой `GET /v1/models` каждого эндпойнта,
  таймаут 5 с на эндпойнт), кэш моделей адаптера пересобирается из живых
  ответов. Провал опроса не роняет страницу — показывается прежний список
  и текст ошибки.
- Корень веб-сервера — директория логов `ADAPTER_DEBUG_LOGPATH`, если она
  задана (там лежат `*.parts` папки сессий); при пустом LOGPATH (zero-config)
  — независимая папка `./tmp/webui`: статус-страница работает, вкладка
  `/session` пуста (per-session логов нет). Папка корня создаётся при
  старте WEBUI.
- Дерево генерируется на лету: если `artefacts/tree.html` устарел или
  отсутствует, `artifact_tree.generate()` пересоздаёт его при заходе на
  страницу. Повторные генерации — **инкрементальные**: `generate()` ведёт
  чекпоинт `artefacts/.build_state.json` и при следующем заходе
  обрабатывает только новые `*.parts` файлы сессии (номер части больше
  чекпоинтного); разметка (Finish/Superseded/Title, ответы на запросы,
  границы страниц) пересчитывается по полному состоянию. Чекпоинт битый
  или отсутствует — холодный полный пересбор с warning. Обновите страницу
  браузера, чтобы увидеть актуальное состояние (активная вкладка при этом
  сохранится — см. ниже).
- Большие сессии дополнительно разбиваются на страницы пагинации:
  `artefacts/pages/<N>/tree.{html,puml,png}` по реальным пользовательским
  запросам; сводный список страниц — `artefacts/pages/index.html` (ссылка
  «по страницам» на вкладке дерева).
- Короткие адреса сессий: имя сессии вида `session-…-<hash8>.parts` можно
  заменить на один hash8 — `/session/<hash8>/<rel_path>` (хеш берётся из
  имени папки сессии; коллизии — первый в списке). Дерево как картинка или
  PlantUML-исходник по короткому пути без знания раскладки артефактов:
  `/session/<session_id|hash8>/png` и `/session/<session_id|hash8>/puml`
  (с `?page=N` — страницу пагинации).
- Runtime-переключение debug-записи без перезапуска адаптера — страница
  `/config`: чекбоксы/числа для узкого пула переменных (4 bool:
  `ADAPTER_DEBUG`, `ADAPTER_DEBUG_TAGS_OUT`, `ADAPTER_DEBUG_TOOLS`,
  `ADAPTER_DEBUG_TOOLS_ERROR`; 3 int: `ADAPTER_TRACE_REASONING_MAX_CHARS`,
  `ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS`, `ADAPTER_DEBUG_TRIM`), POST применяет
  через `config.set_runtime_config()` и показывает, что применилось.
  Переменные пула читаются кодом «на лету» (`config.ADAPTER_X`), поэтому
  изменения видны сразу; сеть/бэкенды/модели/порты на лету не меняются.
- Вкладки `/session`: слева в панели — ссылки «← статус» на страницу `/`
  и «config» на `/config`; активная вкладка запоминается в `location.hash` —
  после обновления страницы открывается та же вкладка, а не первая.
- Стандартный запуск вне процесса адаптера (без данных адаптера — на
  статус-странице будет пометка): `python -m backend_adapter.webserver [КОРЕНЬ] [--port] [--host]`.
