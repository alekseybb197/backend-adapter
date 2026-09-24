# Logging — Полный список лог-блоков backend-adapter

Все лог-блоки разделены на три механизма:

| Механизм | Куда пишется | Функция |
|---|---|---|
| **Debug blocks** `[...]` | `<ADAPTER_DATA_ROOT>/log/session-<ts>-<sid>.log` | `_dr(req_id, "[NAME] ...")` |
| **Console-only blocks** | Stdout/stderr | `_d("[NAME] ...")` |
| **Structured traces** | `<ADAPTER_DATA_ROOT>/log/session-<ts>-<sid>.jsonl` | `_trace(...)` |

Также дополнительные механизмы: **JSON/YAML дампы частей** per-session (вместе с
логами по `ADAPTER_DEBUG_ENABLE`, v0.9.10),
**.err-файлы инцидентов и WARN-событий** (безусловный канал, v0.9.0/v0.9.1 — см. ниже)
и **JSON-файл результата опроса списка моделей** в `var/` (безусловный канал,
v0.9.0 — `<бэкенд>.models.json`, см. ниже).
Отдельно в `var/` лежит **PID-файл** `adapter.pid` (пишется при любом запуске,
v0.9.5, `daemon.py`) — не лог, а служебный артефакт для управления процессом.

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

Debug-логи, trace-логи и `.parts`-дампы пишутся в **лог-папку**
`ADAPTER_DATA_ROOT/log` (v0.9.9; при незаданной/пустой env корень данных —
дефолт `./tmp/adapter`, т.е. `./tmp/adapter/log`).
**Консольные** debug-блоки печатаются **всегда** (v0.8.6) и **обрезаются** до
`ADAPTER_DEBUG_TRIM` символов (`0` — без обрезки); **файловая запись**
включена только при `ADAPTER_DEBUG_ENABLE=1` (при `0` — диск не используется,
папки `log/` и `var/` внутри корня данных всё равно создаются на старте) и пишет в `session-*.log`
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
| 13 | `[TOOL_RESULT]` (content) | ← CLIENT | всегда | Полный JSON content каждого результата (ошибки НЕ выделяются отдельным блоком). В консоли — с обрезкой `ADAPTER_DEBUG_TRIM`; в файл при `ADAPTER_DEBUG_ENABLE=1` — полный. Дополнительно: при `ADAPTER_DEBUG_ENABLE=1` пишутся JSON/YAML-файлы per-session |
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
| 19 | `[RESPONSE]` (stream) | → CLIENT | всегда | Агрегированный snapshot стримированного ответа: text, reasoning, tool_uses, длины (в консоли — с обрезкой `ADAPTER_DEBUG_TRIM`). Дополнительно: при `ADAPTER_DEBUG_ENABLE=1` пишутся JSON/YAML-файлы per-session |

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
| 24 | `[RETRY]` | INTERNAL | всегда | Тайминги повторов попытки; отдельной строкой — `reasoning budget exhausted, max_tokens {N} -> {M}` при подъёме `max_tokens` (v0.9.9, §[`docs/environment.md`](environment.md) §2) |
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
агента (`POST /v1/messages` → `server.do_POST`), независимо от `ADAPTER_DEBUG_ENABLE`
и `ADAPTER_DEBUG_TRIM`.

| Свойство | Значение |
|---|---|
| Функция | `write_error_file(session_id, req_id, *, final_status, backend_url, model, out_body, err_body)` в `session_log.py` |
| Имя файла | `session-<YYYYMMDD-HHMMSS>-<session_id[:8]>.err` — **общий** `_session_file_ts` сессии (тот же ts, что у `session-*.log`/`.jsonl`), та же лог-папка `ADAPTER_DATA_ROOT/log` |
| Когда пишется | Финальный статус клиенту 4xx/5xx: код HTTPError последней попытки (не-retry 4xx, исчерпанные 429/502/503/504), `504` после таймаутов, `502` после прочих ошибок. Один `.err` на запрос (не на попытку). С v0.9.7 сюда же — **обрыв потока mid-stream** (см. ниже) |
| Когда НЕ пишется | 200-успех (кроме WARN-блоков, §«WARN-события») |
| Содержимое | Шапка-метаданные (`session_id`, `final_status`, `model`, `backend_url`), **ПОЛНОЕ** тело запроса к бэкенду (`out_body`), **ПОЛНОЕ** сообщение об ошибке последней попытки (`err_body`). Без обрезки по `ADAPTER_DEBUG_TRIM` |
| Санитайзер | Секреты маскируются `redact()` по умолчанию; при `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` — полные данные |
| Гейт записи | Только наличие лог-папки `ADAPTER_DATA_ROOT/log` (корень всегда непуст, дефолт `./tmp/adapter`); **не** зависит от `ADAPTER_SESSIONS_TABLE` |

**Обрыв потока mid-stream (v0.9.7).** Сбой бэкенда/чтения **после**
`_start_sse(200)` — заголовки `200` и, возможно, часть SSE-событий уже ушли
клиенту, откат на JSON-ответ невозможен (адаптер досылает лишь SSE-событие
`error` в родном формате входа и помечает trace `failed_mid_stream=True`).
Раньше такой обрыв в `.err` не попадал; теперь в файл пишется ERROR-блок с
`final_status=200` и `err_body` с префиксом **`mid-stream abort:`** — по нему
инцидент отличим от честного ответа 200. Счётчик «Ошибок» строки `/sessions`
инкрементится. Три пути: passthrough-релей, конвертеры `responses→completions`
и `messages→completions`.

Формат записи (в духе session-лога, timestamp-строки с префиксом `[req_id]`):

```
==================== ERROR ====================
[2026-09-08T12:00:00] [req_id] session_id=... final_status=400 model=... backend_url=...
[2026-09-08T12:00:00] [req_id] [REQUEST] <полное out_body>
[2026-09-08T12:00:00] [req_id] [BACKEND_ERROR] <полное err_body>
==================== END ERROR ====================
```

Опрос списка моделей (config/webui_status) в `.err` НЕ пишется — у него свой
консольный лог и JSON-дамп. Файл ошибок — наблюдательный канал: провал
записи никогда не роняет обработку запроса.

### Ошибки уровня адаптера в `.err` (v0.9.5, задача 7)

Раньше `.err` покрывал только инциденты взаимодействия с бэкендом (раздел
выше). С v0.9.5 в тот же файл пишутся ошибки уровня адаптера, не дошедшие до
бэкенда: `400` валидации тела (Invalid JSON / нет `model` / strict-модель),
`404` выключенного входа (`ADAPTER_*_TARGET=none`), `400` reject
маршрута (нереализованная конверсия), `501` списка моделей.

**Полнота канала (v0.9.7).** Право запроса на `.err`-блок определяется
флагом `_req_ctx.err_eligible` (распознан входной путь), а **не** наличием
строки в таблице Sessions: при `ADAPTER_SESSIONS_TABLE=0` (учёт выключен)
ошибки всё равно пишутся в `.err`. Счётчик «Ошибок» строки `/sessions`
остаётся привязан к живой таблице — при её отключении он не ведётся, но
`.err` пишется. Дополнительно покрыты:

- `500` — необработанное исключение в `do_POST` (верхнеуровневый `except`);
  если ответ ещё не начат, клиент получает JSON-`500`, и блок пишется;
- `501` — `GET /v1/models`, пока список моделей не прогрет, и **любой
  неподдерживаемый HTTP-метод** (PUT/DELETE/PATCH/OPTIONS/TRACE — теперь
  отвечают JSON-`501` вместо HTML `send_error`, см. `server._unsupported_method`);
- обрыв потока mid-stream (см. выше) — блок с `final_status=200` и пометкой
  `mid-stream abort`.

Инвариант: **любая ошибка распознанного входа даёт ERROR-блок в `.err`**;
счётчик «Ошибок» строки `/sessions` (когда таблица включена) служит прямой
ссылкой на этот файл.

| Свойство | Значение |
|---|---|
| Функция | `write_session_error(session_id, req_id, *, final_status, message, model="", in_body="")` в `session_log.py` |
| Отличие от инцидента | Нет `backend_url` (запрос к бэкенду не уходил); `[REQUEST]` несёт **входящее** тело агента (`in_body`), а не `out_body`; сообщение помечено `[ADAPTER_ERROR]` |
| Совместимость | Блоки самоделимитированы (`==== ERROR ====` / `==== END ERROR ====`), поэтому в одном `.err` соседствуют ERROR-блоки обоих видов и WARNING-блоки |
| Гейт записи | Как у инцидента — только наличие лог-папки `ADAPTER_DATA_ROOT/log`; не зависит от `ADAPTER_DEBUG_ENABLE`/`TRIM` и от пер-сессионного Log |

Формат блока ошибки уровня адаптера:

```
==================== ERROR ====================
[2026-09-12T12:00:00] [req_id] session_id=... final_status=400 model=...
[2026-09-12T12:00:00] [req_id] [REQUEST] <полное входящее тело агента>
[2026-09-12T12:00:00] [req_id] [ADAPTER_ERROR] <сообщение об ошибке>
==================== END ERROR ====================
```

### Пер-сессионный гейт логирования (v0.9.5, снимок — v0.9.8)

Действующий флаг файловой записи сессии читается **пер-сессионно**:
`session_log.logging_enabled(session_id)`. Он гейтит **всё** файловое о сессии —
debug-логи, трейсы и `*.parts`-дампы (v0.9.10:
отдельного гейта частей `parts_enabled` больше нет — части собираются вместе с
логами, «Parts ⊆ Log» стало тождеством). Запрос без идентификатора сессии
(`session_id` пуст или равен `unknown` — ни одного заголовка из
`ADAPTER_SESSION_HEADER`) файлов на диске **не создаёт**: `logging_enabled`
возвращает `False`. Консольный канал это не затрагивает — он безусловен (см.
начало раздела).

**Две модели наследования (v0.9.8).** Значение Log для сессии берётся
**снимком** глобального тумблера в момент **образования сессии**
(`session_settings.ensure_session`, первое обращение к `session_id` — самая
ранняя точка запроса, до первого `_d`/`_trace`/дампов). Дальше сессия живёт
**своим** значением:

- глобальный `ADAPTER_DEBUG` **не включает и не выключает** функционал — он
  лишь шаблон для **новых** сессий: при глобальном Log=on новая сессия
  создаётся с Log=on, при off — с Log=off;
- смена глобального тумблера на `/config` **уже работающие сессии не трогает**
  (управление ими — на `/sessions`, выпадающий список Log, ровно два
  положения `on`/`off`; состояния `inherit` у этого поля нет);
- «сбросить» Log у сессии = взять свежий снимок текущего общего тумблера
  (`set_config(..., clear=...)`), а не «вернуть живую связь».

Отличие от TARGET-полей: у них наследование **живое** (два состояния — не
задано / конкретное значение; «вернуться к общему» = снять переопределение,
v0.9.9; см. `docs/routing.md` §2.4).

На безусловные каналы (`.err`, WARN-блоки, JSON-файлы проверок) пер-сессионный
гейт **не влияет** — они пишутся при любом `ADAPTER_DEBUG`.

---

## WARN-события в `.err`-файл сессии (v0.9.1)

**Тот же безусловный канал**, что и инциденты (раздел выше): WARNING-блоки
пишутся в **тот же** `session-<ts>-<safe8>.err` (общий `_session_file_ts`
сессии), вне `ADAPTER_DEBUG_ENABLE`/`TRIM`, санитайзер тот же
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

## JSON-файл результата опроса списка моделей в `var/` (v0.9.0)

**Безусловный наблюдательный канал** — результат опроса бэкенда на список
моделей пишется плоским JSON-файлом в папку состояния
`ADAPTER_DATA_ROOT/var` (рядом с `model-usage.yaml`, `state.yaml` и PID-файлом),
независимо от `ADAPTER_DEBUG_ENABLE` и
`ADAPTER_DEBUG_TRIM` — как канал `.err` выше.

| Свойство | Значение |
|---|---|
| Модуль | `backend_adapter/probe_json.py` (лист DAG: импортируется `config.py`; на верхнем уровне — stdlib only) |
| Имя файла | `<имя_бэкенда>.models.json` — результат опроса бэкенда `GET /v1/models` (единственная проверка бэкенда, v0.9.9) |
| Когда пишется | При КАЖДОМ опросе бэкенда на модели: стартовый `_init_multi_backends` (config.py), фоновая `refresh_models` (кнопка «⟳ Перепроверить» WEBUI / старт адаптера / первый GET `/`), reload-перечитывания YAML. Упавший бэкенд — тоже файл (`"ok": false` + `"error"`) |
| Перезапись | Каждый файл при новой проверке ПЕРЕЗАПИСЫВАЕТСЯ целиком (атомарно: tmp + `os.replace`, .tmp-хвостов не остаётся) |
| Содержимое | `{"backend", "checked_at", "ok", "count", "models": [полные записи ответа /v1/models]}`; при ошибке — `{"backend", "checked_at", "ok": false, "error": "<текст>"}` |
| Санитайзер | Секреты маскируются `redact()` по умолчанию (по всему дампу); при `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` — полные данные (живое чтение `config`) |
| Гейт записи | Только наличие корня данных `ADAPTER_DATA_ROOT` (всегда непуст, дефолт `./tmp/adapter`; папка `var/` создаётся при записи) |

Функция модуля: `write_models_json(backend_name, payload)` →
`<ADAPTER_DATA_ROOT>/var/<бэкенд>.models.json`.
Дамп — наблюдательный канал: любая ошибка записи молча глотается, проверку
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

---

## JSON / YAML дампы частей per-session — вместе с логами (v0.9.10)

Отдельного флага дампов больше нет: части протокола собираются **вместе с
логами** по единственному мастер-выключателю `ADAPTER_DEBUG_ENABLE`
(пер-сессионно — снимок `ADAPTER_DEBUG`, см. «Пер-сессионный гейт» выше).
В v0.9.10 удалён старый `ADAPTER_DEBUG_PARTS` — если он задан, на старте
печатается одна строка `[WARN]`, значение не читается.

| Переменная | Default | Описание |
|---|---|---|
| `ADAPTER_DEBUG_ENABLE` | `0` (выключено) | **Мастер-выключатель файловой записи**: debug-логи, trace-логи и `*.parts`-дампы частей протокола. Для **каждой** логгируемой части — **пара файлов**: `.json` (машиночитаемый, `json.dumps(indent=2)`) и `.yaml` (человекочитаемый, `yaml.dump(LiteralDumper)`); пишутся функцией `write_debug_json(session_id, tag, data)` из `session_log.py` в `<сессия>.parts/` внутри `ADAPTER_DATA_ROOT/log`. |

**Теги дампов** (все логгируемые части, полные, без обрезки): `BODY`, `TOOL_RESULT`, `OPENAI_BODY`, `FETCH_RAW`, `RESPONSE`. Тег `TOOL_RESULT_ERROR` удалён (v0.8.6) — content ошибок идёт единым `TOOL_RESULT`.

**Поведение:**
- JSON/YAML файлы пишутся в подпапку `<сессия>.parts/` лог-папки `ADAPTER_DATA_ROOT/log` (рядом с `session-*.log`/`.jsonl`).
- Дампы подчинены `ADAPTER_DEBUG_ENABLE` (мастер-выключатель **файловой** записи): при `0` не пишутся.
- Гейт дампов — **сессионный** (`logging_enabled(session_id)`, v0.9.8) и стоит на каждой точке записи, а не читает глобальный `config.ADAPTER_DEBUG` напрямую. Поэтому сессия может собирать части, пока глобальный флаг выключен, и наоборот. Дамп пишется **инкрементально** — пара файлов на каждую часть по мере её поступления (`write_debug_json` вызывается из обработчиков тегов), постфактум-разбора `session-*.log` не существует.
- Запрос без идентификатора сессии дампов не создаёт (файловая запись — сессионная функция).
- JSON-дамп `RESPONSE` при stream-режиме также пишется агрегированный snapshot (см. `streaming.py`).

---

## Structured trace events — `<ADAPTER_DATA_ROOT>/log/session-*.jsonl`

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
| `OPENAI_BODY` (JSON/YAML) | То же самое, в отдельном JSON/YAML-файле по сессии (`ADAPTER_DEBUG_ENABLE=1`) |
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
- Проверка бэкендов (опрос списка моделей `GET /v1/models`)
  запускается **по кнопке-иконке 🔃 «Перепроверить бэкенды» (POST `/`)**,
  при старте
  адаптера и на первом заходе на страницу (GET `/`), и выполняется
  **в фоновом потоке**: `config.start_refresh` → `config.refresh_models()`
  (живой `GET /v1/models` каждого бэкенда, таймаут 10 с на бэкенд), кэш
  моделей адаптера пересобирается из живых ответов; кнопка дополнительно
  **перечитывает** `ADAPTER_BACKEND_CONFIG` (бэкенды добавляются/удаляются
  без рестарта; битый YAML — прежние остаются). Загрузка страницы (GET `/`)
  проверку **не запускает** — она
  рендерит последний результат; пока проверка идёт, страница показывает
  баннер «Проверка выполняется…» и сама перезагружается по завершении
  (JS опрашивает JSON-эндпоинт `/api/refresh-state`). Провал опроса не
  роняет страницу — показывается прежний список и текст ошибки.
- Корень веб-сервера — `ADAPTER_DATA_ROOT` (по умолчанию `./tmp/adapter`).
  Содержимое разложено по подпапкам (v0.9.9): `log/` — `*.parts` папки сессий,
  `session-*.log`/`.jsonl`, `.err`; `var/` — `model-usage.yaml`, `state.yaml`,
  PID-файл `adapter.pid` (v0.9.5 — пишется при любом запуске) и JSON-файл
  результата опроса списка моделей `<бэкенд>.models.json` (безусловный
  канал, см. выше).
  Обе подпапки создаются при старте всегда; per-session логов в `log/` нет,
  пока файловая запись выключена (`ADAPTER_DEBUG_ENABLE=0`) — вкладка
  `/session` пуста.
- Дерево генерируется на лету: если `artefacts/tree.html` устарел или
  отсутствует, `artifact_tree.generate()` пересоздаёт его при заходе на
  страницу. Повторные генерации — **инкрементальные**: `generate()` ведёт
  чекпоинт `artefacts/.build_state.json` и при следующем заходе
  обрабатывает только новые `*.parts` файлы сессии (номер части больше
  чекпоинтного); разметка (Finish/Superseded/Title, ответы на запросы,
  границы страниц) пересчитывается по полному состоянию. Чекпойнт битый
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
  `/config`: чекбоксы/числа для узкого пула переменных (5 bool:
  `ADAPTER_DEBUG`, `ADAPTER_SENSITIVE_LOGGING_ENABLE`,
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
