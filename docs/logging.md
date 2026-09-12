# Logging — Полный список лог-блоков backend-adapter

Все лог-блоки разделены на три механизма:

| Механизм | Куда пишется | Функция |
|---|---|---|
| **Debug blocks** `[...]` | `<LOGPATH>/session-<ts>-<sid>.log` | `_dr(req_id, "[NAME] ...")` |
| **Console-only blocks** | Stdout/stderr | `_d("[NAME] ...")` |
| **Structured traces** | `<LOGPATH>/session-<ts>-<sid>.jsonl` | `_trace(...)` |

Также дополнительные механизмы: **JSON/YAML дампы** per-session через `ADAPTER_DEBUG_PARTS`,
**.err-файлы инцидентов и WARN-событий** (безусловный канал, v0.9.0/v0.9.1 — см. ниже)
и **JSON-файлы результатов проверок бэкендов** в LOGPATH (безусловный канал,
v0.9.0 — `<бэкенд>.models.json` и `<бэкенд>.<модель>.<эндпоинт>.json`, см. ниже).

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

## Debug blocks `[...]` — консоль всегда (с TRIM); файлы при `ADAPTER_DEBUG_ENABLE=1` (полные)

Debug-логи, trace-логи и `.parts`-дампы пишутся в **одну директорию** —
`ADAPTER_DEBUG_LOGPATH` (при незаданной/пустой env — дефолт `./tmp/logs`).
**Консольные** debug-блоки печатаются **всегда** (v0.8.6) и **обрезаются** до
`ADAPTER_DEBUG_TRIM` символов (`0` — без обрезки); **файловая запись**
включена только при `ADAPTER_DEBUG_ENABLE=1` (при `0` — диск не используется,
папка как корень WEBUI всё равно создаётся на старте) и пишет в `session-*.log`
**ПОЛНЫЕ строки без обрезки** — файловый канал принципиально несёт части
полностью (v0.8.6-реформа). Заданный путь — директория; создаётся при
необходимости. Формат имени сессионного файла —
`session-<YYYYMMDD-HHMMSS>-<session_id>.<ext>` (`.log` — debug, `.jsonl` — trace).
`session_id` — первый непустой кандидат из списка `ADAPTER_SESSION_HEADER`
(дефолт `X-Claude-Code-Session-Id,x-opencode-session,x-codex-turn-metadata:session_id`;
элемент `Имя:ключ` — JSON-заголовок), иначе `unknown` —
см. `docs/environment.md` §5 и подраздел «Настройка агента (QwenCode)».

### ← CLIENT — Данные от клиента

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 1 | `[REQ]` | ← CLIENT | всегда | HTTP метод, путь, session_id |
| 2 | `[BODY]` | ← CLIENT | всегда | Тело Anthropic-запроса клиента: в консоли — с обрезкой `ADAPTER_DEBUG_TRIM`; в файл при `ADAPTER_DEBUG_ENABLE=1` — полное |

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
| 11 | `[OPENAI_BODY]` | → BACKEND | всегда | Тело [OI]-запроса, отправляемого в бэкенд: в консоли — с обрезкой `ADAPTER_DEBUG_TRIM`; в файл при `ADAPTER_DEBUG_ENABLE=1` — полное |

---

### ← CLIENT — Tool result обработка

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 12 | `[TOOL_RESULT]` (summary) | ← CLIENT | всегда | `tool_name`, `tool_use_id`, `parent_req_id`, `is_error`, `len(content)` |
| 13 | `[TOOL_RESULT]` (content) | ← CLIENT | всегда | Полный JSON content каждого результата (ошибки НЕ выделяются отдельным блоком). В консоли — с обрезкой `ADAPTER_DEBUG_TRIM`; в файл при `ADAPTER_DEBUG_ENABLE=1` — полный. Дополнительно: при `ADAPTER_DEBUG_PARTS=1` пишутся JSON/YAML-файлы per-session |
| 14 | *(удалён, v0.8.6)* | — | — | `[TOOL_RESULT_ERROR]` больше не выводится: полный content идёт единым блоком для всех результатов (см. 13); тег исчез из кода и дампов |

### → BACKEND — FETCH (request phase, отправка запроса)

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 15 | `[FETCH]` | INTERNAL | всегда | Префикс "(stream)" или нет, номер попытки, retry count, таймаут |

### ← BACKEND — Ответ от бэкенда

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 16 | `[FETCH_RAW]` | ← BACKEND | всегда | Сырой ответ бэкенда (OI-format), до конвертации: в консоли — с обрезкой `ADAPTER_DEBUG_TRIM`; в файл при `ADAPTER_DEBUG_ENABLE=1` — полный |
| 17 | `[FETCH]` (success) | INTERNAL | всегда | Elapsed time, HTTP status, размер ответа в байтах |

### → CLIENT — Конвертация и отправка ответа

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 18 | `[RESPONSE]` (non-stream) | → CLIENT | всегда | Полностью преобразованный Anthropic-ответ: в консоли — с обрезкой `ADAPTER_DEBUG_TRIM`; в файл при `ADAPTER_DEBUG_ENABLE=1` — полный |
| 19 | `[RESPONSE]` (stream) | → CLIENT | всегда | Агрегированный snapshot стримированного ответа: text, reasoning, tool_uses, длины (в консоли — с обрезкой `ADAPTER_DEBUG_TRIM`). Дополнительно: при `ADAPTER_DEBUG_PARTS=1` пишутся JSON/YAML-файлы per-session |

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

## .err-файлы инцидентов взаимодействия с бэкендом (v0.9.0)

**Безусловный наблюдательный канал** — файл ошибок пишется при финальном
ответе клиенту **4xx/5xx** (после ретраев/таймаутов) реального прокси-запроса
агента (`POST /v1/messages` → `server.do_POST`), независимо от `ADAPTER_DEBUG_ENABLE`,
`ADAPTER_DEBUG_PARTS` и `ADAPTER_DEBUG_TRIM`.

| Свойство | Значение |
|---|---|
| Функция | `write_error_file(session_id, req_id, *, final_status, backend_url, model, out_body, err_body)` в `session_log.py` |
| Имя файла | `session-<YYYYMMDD-HHMMSS>-<session_id[:8]>.err` — **общий** `_session_file_ts` сессии (тот же ts, что у `session-*.log`/`.jsonl`), та же директория `ADAPTER_DEBUG_LOGPATH` |
| Когда пишется | Финальный статус клиенту 4xx/5xx: код HTTPError последней попытки (не-retry 4xx, исчерпанные 429/502/503/504), `504` после таймаутов, `502` после прочих ошибок. Один `.err` на запрос (не на попытку) |
| Когда НЕ пишется | 200-успех; ошибки **ДО** бэкенда (нет `/v1/messages`, Invalid JSON, нет `model`, strict-400) — бэкенд не участвовал, инцидента взаимодействия нет |
| Содержимое | Шапка-метаданные (`session_id`, `final_status`, `model`, `backend_url`), **ПОЛНОЕ** тело запроса к бэкенду (`out_body`), **ПОЛНОЕ** сообщение об ошибке последней попытки (`err_body`). Без обрезки по `ADAPTER_DEBUG_TRIM` |
| Санитайзер | Секреты маскируются `redact()` по умолчанию; при `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` — полные данные |
| Гейт записи | Только наличие лог-директории `ADAPTER_DEBUG_LOGPATH` (всегда непуста, дефолт `./tmp/logs`) |

Формат записи (в духе session-лога, timestamp-строки с префиксом `[req_id]`):

```
==================== ERROR ====================
[2026-09-08T12:00:00] [req_id] session_id=... final_status=400 model=... backend_url=...
[2026-09-08T12:00:00] [req_id] [REQUEST] <полное out_body>
[2026-09-08T12:00:00] [req_id] [BACKEND_ERROR] <полное err_body>
==================== END ERROR ====================
```

Дымовые пробы (config/webui_status) в `.err` НЕ пишутся — у них свои
консольные логи. Файл ошибок — наблюдательный канал: провал записи никогда
не роняет обработку запроса.

---

## WARN-события в `.err`-файл сессии (v0.9.1)

**Тот же безусловный канал**, что и инциденты (раздел выше): WARNING-блоки
пишутся в **тот же** `session-<ts>-<safe8>.err` (общий `_session_file_ts`
сессии), вне `ADAPTER_DEBUG_ENABLE`/`PARTS`/`TRIM`, санитайзер тот же
(redact по умолчанию, полные данные при `ADAPTER_SENSITIVE_LOGGING_ENABLE=1`),
провал записи никогда не роняет запрос. В отличие от ERROR-блока у WARN **нет**
`final_status` — предупреждение наблюдается и на **успешном ответе (200)**.
Один запрос может нести в одном `.err` и WARNING-, и ERROR-блок (инвариант
нарушен И запрос позже упал в 4xx/5xx) — блоки самоделимитированы.

| Свойство | Значение |
|---|---|
| Функция | `write_warn_file(session_id, req_id, *, backend_url, model, out_body, warn_body)` в `session_log.py` (v0.9.1) |
| Имя файла | Тот же `session-<YYYYMMDD-HHMMSS>-<session_id[:8]>.err`, что и у инцидентов (общий `_session_file_ts`/дескриптор) |
| Триггер 1 — `[WARN] First message is NOT system: <role>` | server.py, convert-ветка: первое сообщение запроса не `system` (инвариант конвертации нарушен). Пишется один раз на запрос, сразу после построения `out_body`/`backend_url` — точка покрывает и stream-, и non-stream-ветки конвертации. `<role>` — роль первого сообщения (`user`, `assistant`, …; `empty` — пустой список) |
| Триггер 2 — `[USAGE_WARN] Backend не вернул usage в стриме — input_tokens оценён эвристически (~N, chars/4), реальное число неизвестно` | streaming.py, `stream_openai_to_anthropic`: бэкенд не прислал `usage` (нет `stream_options.include_usage` / флаг `ADAPTER_STREAM_INCLUDE_USAGE=0`) и `input_tokens` оценён `approx_prompt_chars // 4`. Пишется только когда вызывающий код (server.do_POST) передал `out_body`/`backend_url`; прямые вызовы функции без этих параметров записи не делают |
| Содержимое | Шапка-метаданные **без статуса** (`session_id`, `model`, `backend_url`), **ПОЛНЫЙ** `[REQUEST]` (`out_body`), `[WARN]` — полный текст события. Без обрезки по `ADAPTER_DEBUG_TRIM` |
| Когда НЕ пишется | Вне двух триггеров выше — прочие диагностические предупреждения (в т.ч. не-стрим «бэкенд без usage» и passthrough-ветки без usage) в `.err` НЕ пишутся, остаются консоль/`session-*.log` |

Формат записи:

```
==================== WARNING ====================
[2026-09-09T12:00:00] [req_id] session_id=... model=... backend_url=...
[2026-09-09T12:00:00] [req_id] [REQUEST] <полное out_body>
[2026-09-09T12:00:00] [req_id] [WARN] <полный текст события>
==================== END WARNING ====================
```

---

## JSON-файлы результатов проверок в LOGPATH (v0.9.0)

**Безусловный наблюдательный канал** — результаты проверок бэкендов пишутся
плоскими JSON-файлами в корень `ADAPTER_DEBUG_LOGPATH` (рядом с
`model-usage.yaml` и корнем WEBUI), независимо от `ADAPTER_DEBUG_ENABLE`,
`ADAPTER_DEBUG_PARTS` и `ADAPTER_DEBUG_TRIM` — как канал `.err` выше.

| Свойство | Значение |
|---|---|
| Модуль | `backend_adapter/probe_json.py` (лист DAG: импортируется `config.py` и `model_usage.py`; на верхнем уровне — stdlib only) |
| Имя файла моделей | `<имя_бэкенда>.models.json` — результат проверки бэкенда на доступные модели |
| Имя файла эндпоинтов | `<имя_бэкенда>.<конверт.модель>.<pname>.json` — результат проверки эндпоинта модели. Конвертация имени модели: замена всех `/` и `:` на `_` (`org/model:v1` → `org_model_v1`); остальные символы не трогаются. `pname` — из `ENDPOINT_PROBES`: `completions \| messages \| responses \| embeddings` |
| Когда пишутся `.models.json` | При КАЖДОЙ проверке бэкенда на модели: стартовый `_init_multi_backends` (config.py), фоновая `refresh_models` (кнопка «⟳ Перепроверить» WEBUI / старт адаптера / первый GET `/`), reload-перечитывания YAML. Упавший бэкенд — тоже файл (`"ok": false` + `"error"`) |
| Когда пишутся эндпоинт-файлы | При КАЖДОЙ фактической пробе эндпоинта: фоновая `probe_endpoints` (config.py, политика «только явно указанные пробы») и пер-модельные пробы usage-таблицы (`_probe_model_endpoints` в model_usage.py — первое обращение модели и reprobe строки). Из кэша файлы НЕ пишутся — свежие дампы только при сетевой проверке |
| Перезапись | Каждый файл при новой проверке ПЕРЕЗАПИСЫВАЕТСЯ целиком (атомарно: tmp + `os.replace`, .tmp-хвостов не остаётся) |
| Содержимое `.models.json` | `{"backend", "checked_at", "ok", "count", "models": [полные записи ответа /v1/models]}`; при ошибке — `{"backend", "checked_at", "ok": false, "error": "<текст>"}` |
| Содержимое эндпоинт-файла | `{"backend", "model", "endpoint", "path", "checked_at", "status", "found", "error"?}`. `status=None` — сетевая ошибка/таймаут (`found=False`); `error` — текст ошибки эндпоинта при её наличии |
| Санитайзер | Секреты маскируются `redact()` по умолчанию (по всему дампу); при `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` — полные данные (живое чтение `config`) |
| Гейт записи | Только наличие лог-директории `ADAPTER_DEBUG_LOGPATH` (всегда непуста, дефолт `./tmp/logs`; создаётся при записи) |

Функции модуля: `write_models_json(backend_name, payload)` →
`<LOGPATH>/<бэкенд>.models.json`; `write_endpoint_json(backend_name, model_id,
pname, payload)` → `<LOGPATH>/<бэкенд>.<конверт.модель>.<pname>.json`.
Дампы — наблюдательный канал: любая ошибка записи молча глотается, проверку
не роняет.

---

## Console-only blocks — `_d("[...] ...")` (только stdout)

| # | Block | Где | Что |
|---|---|---|---|
| 1 | `[CLIENT_GONE]` | `server.py:57` | Socket-level client gone |
| 2 | `[HTTP]` | `server.py:69` | Generic HTTP handler log |
| 3 | `[CLIENT_GONE]` | `server.py:84` | Client gone during JSON send |
| 4 | `[WARN]` | `server.py:86` | Ошибка отправки ответа клиенту |
| 5 | `[INIT]` | `backend-adapter.py:108` | Старт адаптера: путь к YAML-конфигу бэкендов |
| 6 | `[ENDPOINT_PROBE]` | `config.py` | Дымовая проба API-эндпойнтов бэкенда: одна строка на фактическую пробу — `backend '<имя>' (<base>): completions=200 messages=404 responses=… embeddings=…` (сырые HTTP-коды; при сетевой ошибке — `failed: <текст>`). Пишется `print`-ом **безусловно** (консольные debug-логи не гейтятся — v0.8.6); кэш-хиты (повторный заход на страницу < 60 с) не логируются — лог даёт историю фактических проб, страница показывает последний результат |

---

## JSON / YAML дампы per-session — `ADAPTER_DEBUG_PARTS`

| Переменная | Default | Описание |
|---|---|---|
| `ADAPTER_DEBUG_PARTS` | `0` (выключено) | **Флаг**: включить per-session дампы частей протокола — **всех логгируемых** (фиксированного списка тегов больше нет, v0.8.6). Для каждого тега пишется **пара файлов**: `.json` (машиночитаемый, `json.dumps(indent=2)`) и `.yaml` (человекочитаемый, `yaml.dump(LiteralDumper)`). Файлы пишутся функцией `write_debug_json(session_id, tag, data)` из `session_log.py`. Требует `ADAPTER_DEBUG_ENABLE=1` и директорию логов `ADAPTER_DEBUG_LOGPATH` (создаётся при необходимости). |

**Теги дампов** (все логгируемые части, полные, без обрезки): `BODY`, `TOOL_RESULT`, `OPENAI_BODY`, `FETCH_RAW`, `RESPONSE`. Тег `TOOL_RESULT_ERROR` удалён (v0.8.6) — content ошибок идёт единым `TOOL_RESULT`.

**Поведение:**
- JSON/YAML файлы пишутся в ту же директорию, что и debug/trace-логи (`ADAPTER_DEBUG_LOGPATH`).
- Дампы подчинены `ADAPTER_DEBUG_ENABLE` (мастер-выключатель **файловой** записи): при `0` не пишутся.
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
| 9 | `response_content` | → CLIENT | `text_len`, `tool_uses`, `finish_reason_raw`, `stop_reason_mapped`, `reasoning_present`, `reasoning_len`, `reasoning`, `streamed` |
| 10 | `usage_report` | ← BACKEND / INTERNAL | `input_tokens`, `input_tokens_estimated`, `output_tokens`, `streamed` |

---

## Сводная матрица по направлениям

### → BACKEND — данные, отправляемые в LLM backend

| Block | Описание |
|---|---|
| `[OPENAI_BODY]` | Тело OpenAI-формата, отправляемое POST-запросом в бэкенд (converted from Anthropic) |
| `OPENAI_BODY` (JSON/YAML) | То же самое, в отдельном JSON/YAML-файле по сессии (`ADAPTER_DEBUG_PARTS=1`) |
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
| `[REQ]` | HTTP metadata: метод, путь, session_id (первый непустой кандидат из списка `ADAPTER_SESSION_HEADER`; элемент `Имя:ключ` — JSON-заголовок; иначе `unknown`) |
| `[BODY]` | Исходное Anthropic-тело запроса клиента |
| `[TOOL_RESULT]` (summary) | `tool_use_id`, `parent_req_id`, `is_error`, `len(content)` (всегда) |
| `[TOOL_RESULT]` (content) | Полный JSON content tool result — безусловно, единым блоком для всех результатов (в консоли с обрезкой `ADAPTER_DEBUG_TRIM`, в файл при `ADAPTER_DEBUG_ENABLE=1` полный) |

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
| Console-only: `[CLIENT_GONE]`, `[HTTP]`, `[WARN]`, `[INIT]` | Системные логи |
| Все trace events | Структурированное журналирование (см. таблицу выше) |

---

## Просмотр сессий в браузере — WEBUI

Веб-интерфейс (общее ядро `backend_adapter/webserver.py` + эндпойнты)
показывает «треки» общения агента и LLM по `.parts`-дампам: для каждой
сессии строится дерево артефактов (`artefacts/tree.html`), страница
открывается во вкладках. Руководство по всем страницам и JSON-эндпоинтам
WEBUI — `docs/webui.md`.

- Поднимается **всегда** — флага отключения нет (v0.8.6; `ADAPTER_WEBUI_ENABLE`
  удалён); порт — `ADAPTER_WEBUI_PORT` (по умолчанию `8765`), слушает на
  `ADAPTER_WEBUI_HOST` (по умолчанию `127.0.0.1` — только локально;
  `0.0.0.0` — доступ из сети).
- Адреса: `http://127.0.0.1:<port>/` — статус-страница (версия кода,
  режим работы, LLM-эндпойнты с доступностью и списком моделей);
  `http://127.0.0.1:<port>/session` — вкладки просмотра сессий;
  `http://127.0.0.1:<port>/config` — runtime-переключение объёма
  debug-записи (см. ниже); `http://127.0.0.1:<port>/api/refresh-state` —
  JSON-состояние фоновой проверки бэкендов (для JS страницы `/`).
- Проверка бэкендов (список моделей + дымовая проба эндпойнтов)
  запускается **по кнопке-иконке 🔃 «Перепроверить бэкенды» (POST `/`)**,
  при старте
  адаптера и на первом заходе на страницу (GET `/`), и выполняется
  **в фоновом потоке**: `config.start_refresh` → `config.refresh_models()`
  (живой `GET /v1/models` каждого эндпойнта, таймаут 10 с на эндпоинт), кэш
  моделей адаптера пересобирается из живых ответов; кнопка дополнительно
  **перечитывает** `ADAPTER_BACKEND_CONFIG` (бэкенды добавляются/удаляются
  без рестарта; битый YAML — прежние остаются). Загрузка страницы (GET `/`)
  проверку **не запускает** — она
  рендерит последний результат; пока проверка идёт, страница показывает
  баннер «Проверка выполняется…» и сама перезагружается по завершении
  (JS опрашивает JSON-эндпоинт `/api/refresh-state`). Провал опроса не
  роняет страницу — показывается прежний список и текст ошибки.
- Корень веб-сервера — директория `ADAPTER_DEBUG_LOGPATH` (по умолчанию
  `./tmp/logs`; там лежат `*.parts` папки сессий, файл `model-usage.yaml`
  и JSON-файлы результатов проверок `<бэкенд>.models.json` /
  `<бэкенд>.<модель>.<эндпоинт>.json` — безусловный канал, см. выше).
  Директория создаётся при старте всегда; per-session логов в ней нет, пока
  файловая запись выключена (`ADAPTER_DEBUG_ENABLE=0`) — вкладка `/session`
  пуста. Папка корня создаётся при старте WEBUI.
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
  `/config`: чекбоксы/числа для узкого пула переменных (6 bool:
  `ADAPTER_DEBUG`, `ADAPTER_DEBUG_PARTS`, `ADAPTER_SENSITIVE_LOGGING_ENABLE`,
  `ADAPTER_STREAMING_ENABLE`, `ADAPTER_STREAM_INCLUDE_USAGE`,
  `ADAPTER_STRICT_MODELS`; 3 int: `ADAPTER_DEBUG_TRIM`,
  `ADAPTER_TRACE_REASONING_MAX_CHARS`, `ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS`),
  POST применяет через `config.set_runtime_config()` и показывает, что
  применилось. Переменные пула читаются кодом «на лету»
  (`config.ADAPTER_X`), поэтому изменения видны сразу; сеть/бэкенды/модели/
  порты на лету не меняются.
- Вкладки `/session`: слева в панели — ссылки «Статус 📊» на страницу `/`
  и «Runtime config 🔧» на `/config`; активная вкладка запоминается в
  `location.hash` — после обновления страницы открывается та же вкладка,
  а не первая.
- Стандартный запуск вне процесса адаптера (без данных адаптера — на
  статус-странице будет пометка): `python -m backend_adapter.webserver [КОРЕНЬ] [--port] [--host]`.
