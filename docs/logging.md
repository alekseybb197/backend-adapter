# Logging — Полный список лог-блоков backend-adapter

Все лог-блоки разделены на три механизма:

| Механизм | Куда пишется | Функция |
|---|---|---|
| **Debug blocks** `[...]` | `session_log/debug-<session>.log` | `_dr(req_id, "[NAME] ...")` |
| **Console-only blocks** | Stdout/stderr | `_d("[NAME] ...")` |
| **Structured traces** | `session_log/trace-<session>.jsonl` | `_trace(...)` |

Также отдельный механизм: **OpenAI JSON dump** → `session_log/<session>.parts/openai-NNNN.json`.

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

## Debug blocks `[...]` — `session_log/debug-*.log`

### ← CLIENT — Данные от клиента

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 1 | `[REQ]` | ← CLIENT | всегда | HTTP метод, путь, session_id |
| 2 | `[BODY]` | ← CLIENT | `ADAPTER_DEBUG_TRIM` / `ADAPTER_DEBUG_BODY_FULL=1` | Полное тело Anthropic-запроса клиента |

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
| 11 | `[OPENAI_BODY]` | → BACKEND | `ADAPTER_DEBUG_OPENAI_BODY_FULL` | Полное тело OpenAI-запроса, отправляемого в бэкенд |

### ← CLIENT — Обработка tool_result от клиента

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 12 | `[TOOL_RESULT]` (summary) | ← CLIENT | всегда | `tool_use_id`, `parent_req_id`, `is_error`, `len(content)` |
| 13 | `[TOOL_RESULT]` (content) | ← CLIENT | `ADAPTER_DEBUG_TOOLS=1` | Полный JSON content tool result |
| 14 | `[TOOL_RESULT_ERROR]` | ← CLIENT | `ADAPTER_DEBUG_TOOLS_ERROR=1` | Снэпшот ошибки: `tool_use_id`, `parent_req_id`, `is_error`, `content` |

### → BACKEND — FETCH (request phase, отправка запроса)

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 15 | `[FETCH]` | INTERNAL | всегда | Префикс "(stream)" или нет, номер попытки, retry count, таймаут |

### ← BACKEND — Ответ от бэкенда

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 16 | `[FETCH_RAW]` | ← BACKEND | `ADAPTER_DEBUG_RESPONSE_FULL` / `stream=False` | Сырой ответ бэкенда (OpenAI format), до конвертации |
| 17 | `[FETCH]` (success) | INTERNAL | всегда | Elapsed time, HTTP status, размер ответа в байтах |

### → CLIENT — Конвертация и отправка ответа

| # | Block | Направление | Опции | Что содержит |
|---|---|---|---|---|
| 18 | `[RESPONSE]` (non-stream) | → CLIENT | `ADAPTER_DEBUG_RESPONSE_FULL` / `stream=False` | Полностью преобразованный Anthropic-ответ |
| 19 | `[RESPONSE]` (stream) | → CLIENT | `stream=True` | Агрегированный snapshot стримированного ответа: text, reasoning, tool_uses, длины |

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
| 6 | `[INIT]` | `backend-adapter.py:102` | Старт multi-backend режима |

---

## Structured trace events — `session_log/trace-*.jsonl`

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

## OpenAI JSON dump — отдельный механизм

| Механизм | Где | Направление | Опции | Что |
|---|---|---|---|---|
| `openai-NNNN.json` | `<session>.parts/openai-NNNN.json` | → BACKEND | `ADAPTER_DEBUG_OPENAI_BODY_JSON=1` | Полный `openai_body` dict — **тело запроса, отправленного в LLM backend** |

---

## Сводная матрица по направлениям

### → BACKEND — данные, отправляемые в LLM backend

| Block | Описание |
|---|---|
| `[OPENAI_BODY]` | Тело OpenAI-формата, отправляемое POST-запросом в бэкенд (converted from Anthropic) |
| `openai-NNNN.json` | То же самое, но в отдельном JSON-файле по сессии |
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
| `[TOOL_RESULT_ERROR]` | Снэпшот ошибки: `tool_use_id`, `parent_req_id`, `is_error`, `content` (`ADAPTER_DEBUG_TOOLS_ERROR=1`) |

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
