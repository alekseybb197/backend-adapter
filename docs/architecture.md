# Architecture — backend-adapter

> Claude Code (Anthropic API) ↔ OpenAI-compatible backend reverse proxy.

---

## 1. Overview

`backend-adapter` is a lightweight HTTP reverse proxy that bridges [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview) (which speaks the Anthropic Messages API) to OpenAI-compatible LLM backends. It runs as a local HTTP server (default port **9999**) and performs three core functions:

1. **Format conversion** — Anthropic ↔ OpenAI messages, tools, tool_choice, system prompts
2. **Streaming passthrough** — SSE (Server-Sent Events) conversion: OpenAI backend SSE → Anthropic client SSE
3. **Observability** — per-session debug logs, structured JSONL trace, secret redaction

Claude Code is configured to route its API traffic through the adapter via `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` environment variables pointing to `http://localhost:9999`.

---

## 2. Directory layout

```
backend-adapter.py          ← entry point (startup: config check, backend init, server)
backend_adapter/
├── __init__.py             ← lazy proxy for module-level globals
├── config.py               ← env vars, model mapping, backend routing, YAML parser, probe (refresh)
├── server.py               ← HTTP handler (Adapter), QuietThreadingHTTPServer
├── convert.py              ← Anthropic ↔ OpenAI conversion functions
├── streaming.py            ← SSE streaming: [OI] SSE → Anthropic SSE (conversion)
│                             + passthrough E→E SSE-relay (relay_sse, v0.9.0 — §4.2/§5.4)
├── tracer.py               ← JSONL trace logging + tool-use causality tracking
├── logger.py               ← human-readable debug logs (_d, _dr)
├── redact.py               ← secret masking (Bearer tokens, *_KEY, *_PAT, etc.)
├── session_log.py          ← per-session log file management with FIFO eviction
├── daemon.py               ← process detachment (double fork + stdio redirect)
├── webserver.py            ← WEBUI core: shared web server, endpoint registry/router,
│                             WebContext, serve(), CLI (python -m backend_adapter.webserver)
├── model_usage.py          ← used-models table (см. §6.6): учёт моделей запросов +
│                             токены usage ответов (input/output) + дымовая проба
│                             эндпоинтов по каждой модели; перепроверка строки
│                             (reprobe); персистентный YAML (version: 2, миграция v1)
├── session_registry.py     ← таблица сессий агентов (секция «Sessions»,
│                             v0.9.2): in-memory, строка = КОРТЕЖ (session,
│                             agent, model, backend, route) — смена модели или
│                             обработчика даёт новую строку (upsert по полному
│                             кортежу), плюс last_seen/calls/errors; БЕЗ
│                             персистентности; лист DAG — импортирует config
├── webui_status.py         ← WEBUI endpoints "/", "/api/refresh-state",
│                             "/api/model-usage/reset", "/api/model-usage/reprobe",
│                             "/api/model-usage/delete", "/api/model-usage/reprobe-state",
│                             "/api/model-usage/snapshot", "/api/sessions/snapshot":
│                             status page (version, LLM endpoints, models) +
│                             background-check state + секция «Models in use»
│                             (live-счётчики, сброс счётчиков/перепроверка/удаление
│                             строки) + секция «Sessions» (live-счётчики)
├── webui_ops.py            ← WEBUI health endpoints "/healthz", "/health", "/live",
│                             "/ready" (200/503 JSON; readiness по _BACKENDS/
│                             _AVAILABLE_MODELS) — см. §6.7
├── webui_config_api.py     ← WEBUI endpoint "/config": runtime-config form (RUNTIME_CONFIG_POOL)
├── prometheus_exporter.py  ← отдельный слушатель метрик /metrics (text exposition
│                             0.0.4, stdlib-only) — см. §6.8
├── session_viewer.py       ← WEBUI endpoint "/session": *.parts session tabs + file serving
├── probe_json.py           ← JSON-результаты проверок бэкендов в LOGPATH
│                             (<бэкенд>.models.json и <бэкенд>.<модель>.<pname>.json,
│                             безусловный канал, redact) — см. §6.9
├── routing.py              ← входные эндпоинты и TARGET-маршрутизация (см. §4.2):
│                             INPUT_PATHS, IMPLEMENTED_CONVERSIONS,
│                             decide() по кэшу проб (лист DAG — импортирует config)
└── artifact_tree.py        ← artifact-tree generator, SPLIT INTO A PACKAGE (below):
    artifact_tree_common.py      ← shared utils: volatility patterns, sha12, text extract, colors
    artifact_tree_registry.py    ← ArtifactRegistry: dedup registry + protocol-id links
    artifact_tree_parse.py       ← part discovery, JSON load, kind classification, inline labels
    artifact_tree_turnbuilder.py ← build_turns: pair openai_body ↔ fetch_raw into turns
    artifact_tree_plantuml.py    ← PlantUML rendering (tree.puml)
    artifact_tree_graphviz.py    ← PNG via real PlantUML or Graphviz fallback
    artifact_tree_html.py        ← interactive tree.html (graph model, dot layout, render)
    artifact_tree.py             ← thin shim: generate(), main(), __all__ re-exports
```

`artifact_tree.py` is kept as a thin re-export shim (public API: `generate()`), so
`from backend_adapter import artifact_tree` and the `session_viewer.py` import keep
working unchanged. The 7 real modules above it live flat in `backend_adapter/`
(the package was intentionally NOT nested in a subdirectory — the repo keeps every
module on one level, see ADR 2026-09-01).

---

## 3. Component diagram

```
[CC] (Anthropic API client)        другие клиенты: [OI]-SDK, агенты, curl
        │                                   │
        │                                   │
        │  POST /v1/messages                │  POST /v1/chat/completions
        │  GET /v1/models                   │  POST /v1/responses
        │  (Anthropic format)               │  ([OI] / Responses format)
        ▼                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ backend-adapter.py                                              │
│                                                                 │
│ (entry: startup backend init, WEBUI/exporters,                  │
│  signal handlers → server.serve_forever())                      │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│ server.py: Adapter                                              │
│                                                                 │
│ do_GET  → /v1/models → _AVAILABLE_MODELS                        │
│ do_POST → input_path_to_format(path) → fmt                      │
│           (messages | completions | responses; вне трёх — 404)  │
│   parse & validate → model обязателен → strict model check      │
│   model mapping (ADAPTER_MODELS_MAPPING) → _resolve_backend     │
│   routing.decide(fmt, backend_name) → action                    │
│     решения по TARGET + кэшу endpoint_support (сети нет)        │
│   ├─ disabled (TARGET=none) / reject                            │
│   │    404 / 400 / 502 JSON  (usage и .err не пишутся —         │
│   │    запрос до бэкенда не дошёл)                              │
│   ├─ passthrough E→E (вход == выход, TARGET/auto)               │
│   │    body["model"] = resolved_model;                          │
│   │    backend_url = base + INPUT_PATHS[out_fmt]                │
│   │    ├─ non-stream → _send_raw (тело бэкенда дословно)        │
│   │    └─ stream     → relay_sse (байты + usage-скан)           │
│   └─ convert (messages→completions — единств. пара)             │
│        ├─ stream     → stream_openai_to_anthropic               │
│        └─ non-stream → convert_openai_to_anthropic              │
│   record_model_usage + usage_tokens — только принятые           │
│   маршруты (после decide); .err на финальный 4xx/5xx            │
│   прокси-запроса (после ретраев)                                │
│                                                                 │
│ Cross-cutting:                                                  │
│   _d()/_dr() → logger + redact   retry loop (backoff)           │
│   _trace()   → tracer + redact   ADAPTER_TIMEOUT                │
│   session_log → per-session files, parts-дампы                  │
└─────────────────────────────────────────────────────────────────┘
        │
        │  POST /v1/chat/completions ([OI] format) — convert / passthrough
        │  POST /v1/messages | /v1/responses    — passthrough E→E
        │  Bearer <key>
        ▼
  [OI]-compatible LLM backend
  (Kaspersky LLM Service, LiteLLM, etc.)
```

---

## 4. Request lifecycle

### 4.1 Startup (`backend-adapter.py:80–122`)

1. If `ADAPTER_DETACH_ENABLE=1` — double-fork daemonize + write PID file
2. Parse env config → `backend_adapter/config.py` (all env vars with `ADAPTER_` prefix)
3. Initialize backends: empty `ADAPTER_BACKEND_CONFIG` → `[FATAL]` + `sys.exit(1)`; parse YAML (`_parse_backend_yaml`), resolve `key` env vars, probe `GET /v1/models` per backend (this startup probe is unconditional — it fills the model list used for strict validation; a backend that fails to respond only logs a `[WARN]` and drops out, but the adapter exits `[FATAL]` if no models were retrieved from any backend), resolve model collisions by prefixing with `<backend_name>.`
4. Start `QuietThreadingHTTPServer` on `ADAPTER_ENDPOINT_HOST:<PROXY_PORT>`
   (`ADAPTER_ENDPOINT_HOST` defaults to `127.0.0.1` — localhost only;
   `0.0.0.0` — all interfaces)
5. Log directory `ADAPTER_DEBUG_LOGPATH` (default `./tmp/logs` when the env var is
   empty/unset — the path is always non-empty; v0.8.6): created unconditionally at
   startup (it doubles as the WEBUI root); **file** logging of sessions/traces/dumps
   only happens at `ADAPTER_DEBUG_ENABLE=1` (master switch of the *file* write —
   console debug logs are unconditional); points at an existing **file** →
   `[FATAL]` + hint + `sys.exit(1)` (the path is always a directory).
6. Start the WEBUI in a daemon thread via
   `webserver.serve(root, __version__)` on `ADAPTER_WEBUI_HOST:<ADAPTER_WEBUI_PORT>`
   (default `127.0.0.1` — localhost only; `0.0.0.0` — access from the network,
   careful with session contents) where `root` = `ADAPTER_DEBUG_LOGPATH` — the WEBUI
   is always started, there is no disable flag (v0.8.6; `ADAPTER_WEBUI_ENABLE`
   removed; `/session` is empty until file logging is enabled and logs exist;
   endpoints: `/` —
   status, `/session` — session viewer, `/config` — runtime-config form,
   `/api/refresh-state` — JSON state of the background check, see §6.5,
   `/api/model-usage/reset` — zeroes a used-model row's counters (row is kept),
   `/api/model-usage/reprobe` — background row re-probe,
   `/api/model-usage/delete` — removes a used-model row from the table and
   the YAML file (not kept),
   `/api/model-usage/reprobe-state` — its JSON state, see §6.6).
   The used-models table persists to `model-usage.yaml` (version: 2, v1 migrated)
   in `root`.
   Loading the status page `/` (GET) renders the current state
   (`config.refresh_state()`: models from the startup probe, or from the last
   check) and — if no check has run yet (`done_at` is empty) — starts the
   **first** check automatically (`webui_status._autostart_first_check`).
   Checks are background (`config.start_refresh` runs `config.refresh_models`,
   5 s timeout per endpoint, in a daemon thread): started at adapter startup,
   on the first GET `/`, and by the 🔃 button «Перепроверить бэкенды» (POST `/`),
   which answers **303 See Other** → GET `/` (PRG pattern — page reloads
   never repeat the POST, no «resubmit» dialog). While a check runs, the page
   shows a «Проверка выполняется…» banner and polls `/api/refresh-state`;
   when the check finishes, JS reloads the page (`location.reload()`), which
   renders the fresh `_AVAILABLE_MODELS`/`_MODEL_TO_BACKEND` caches — models
   added by the backend after startup are picked up without restarting the
   adapter.
   The `/config` endpoint toggles the runtime debug-write pool
   (`config.get_runtime_config`/`set_runtime_config`, see §8.6) without a restart.

### 4.2 Входные POST-эндпоинты и TARGET-маршрутизация (do_POST, server.py)

Адаптер принимает **три** POST-входа: `/v1/messages` (Anthropic Messages API),
`/v1/chat/completions` ([OI] Chat Completions), `/v1/responses` ([OI] Responses
API) — пути зеркалят `config.ENDPOINT_PROBES`. Что делать с запросом на каждом
входе решает **TARGET-маршрутизация** (`backend_adapter/routing.py`, лист DAG,
импортирует только config; его импортирует только server.py): три
env-переменные `ADAPTER_MESSAGES_TARGET` (дефолт `completions`),
`ADAPTER_COMPLETIONS_TARGET` и `ADAPTER_RESPONSES_TARGET` (дефолт `none`) —
префикс = входной эндпоинт, значение ∈
`completions|messages|responses|auto|none`. **Принципы настройки, матрица
«вход × значение» и перспективы — [`docs/routing.md`](routing.md).**

`routing.decide(inp, backend_name)` возвращает `(action, out_fmt, msg, status)`
по **кэшу проб** `config.endpoint_support(backend_name, pname)` (геттер
`_ENDPOINT_STATE`; сети в запросе нет — None трактуется как False):
action ∈ {passthrough (E→E: входной формат == целевому и бэкенд его
поддерживает), convert (только реализованные пары — сегодня
`messages→completions`), reject (нереализованная конверсия → 400 «conversion …
is not implemented»; `auto` без маршрута → 400 «no route»; явный passthrough/
convert при не поддерживающем бэкенде → 502), disabled (TARGET=none → 404)}.
В `auto` приоритет: passthrough E→E (для messages в т.ч. `messages→messages`)
> реализованная конверсия > 400. Реестр реализованных пар —
`IMPLEMENTED_CONVERSIONS` в routing.py.

Общий конвейер (для всех трёх входов; disabled/reject уходят ответом ДО
обращения к бэкенду — usage-учёт и `.err` не пишутся):

```
1. Extract session_id, req_id, update session_log context
2. Input-path → format (routing.input_path_to_format); прочие пути → 404
   (строку сессии НЕ создаёт — учёт стоит после этого return)
3. Parse & validate request JSON (require "model" field) — единообразно для входов
   - 400-ветки (Invalid JSON / Missing model) — учёт сессии
     (session_registry.register с ПУСТЫМИ model/backend/route: до routing.decide
     не дошли; v0.9.2, см. §6.10)
4. Strict model validation (ADAPTER_STRICT_MODELS) — 400 strict также учитывается
   пустым кортежем
5. Record usage of the client model (model_usage.record_model_usage — used-models
   table; at first use of a model this synchronously smoke-probes that model's
   endpoints, see §6.6; on every exit path do_POST's finally accumulates the
   backend usage tokens via model_usage.add_usage_tokens; accounting never
   affects the request)
6. Model mapping (ADAPTER_MODELS_MAPPING string → dict)
7. Backend resolution (_resolve_backend)
   - Explicit prefix (<backend>.model) → strip, route
   - Lookup in _MODEL_TO_BACKEND
   - Fallback → _DEFAULT_BACKEND
8. routing.decide(fmt, backend_name) → action/out_fmt
   - учёт сессии (session_registry.register по полному кортежу session+agent+
     model+backend+route — включая reject/disabled: видно, куда агент пытался;
     upsert, calls+1, эвикция по ADAPTER_SESSIONS_TABLE; v0.9.2, см. §6.10)
   - disabled/reject → JSON-ответ (404/400/502), return (запрос до бэкенда не дошёл)
9. Only messages→completions conversion: trace tool_results from incoming messages
   (causality: tool_use_id → parent req_id); convert Anthropic → [OI] (messages,
   tools, tool_choice, system)
   Passthrough E→E: тело как пришло — мутация body["model"] = resolved_model
   (и stream:false при ADAPTER_STREAMING_ENABLE=0); конвертеры не участвуют,
   поля запроса не валидируются (бэкенд ответит 400 сам). Исключение v0.9.2:
   только для messages→messages все role=system переносятся в начало
   (normalize_messages_system_first, convert.py — перенос без склейки,
   порядок остальных ролей сохраняется; system уже первым / нет system —
   без изменений). Прочие passthrough-пути (completions→completions,
   responses→responses) уходят дословно — там нет инварианта «system первым»
10. Determine stream mode (client stream flag × ADAPTER_STREAMING_ENABLE)
11. Backend URL: base + путь формата выхода (INPUT_PATHS[out_fmt]; конверсия
    messages→completions — по-прежнему /v1/chat/completions)
12. Retry loop (ADAPTER_RETRY times, exponential backoff):
   ├─ Stream branch:
   │  ├─ conversion: urllib urlopen → _start_sse() → stream_openai_to_anthropic()
   │  │   └─ Chunk-by-chunk SSE conversion, write Anthropic SSE events to wfile
   │  └─ passthrough E→E: urllib urlopen → _start_sse() → relay_sse()
   │      └─ Вербатим-релей байтов бэкенда клиенту (без пере-фрейминга); по пути —
   │         скан SSE-строк на usage (см. §5.4); конец потока — по EOF бэкенда
   │         (адаптер уже отдал Connection: close клиенту)
   └─ Non-stream branch:
      ├─ conversion: urllib urlopen → read full → convert_openai_to_anthropic()
      │   └─ Single JSON response → _send_json()
      └─ passthrough E→E: тело ответа бэкенда дословно → _send_raw(200, ...)
13. Usage tokens из ответа по формату выхода: completions — usage.prompt_tokens/
    completion_tokens; responses/messages — usage.input_tokens/output_tokens
14. Error handling:
    ├─ HTTPError (retry on 429/502/503/504 only)
    ├─ TimeoutError (retry)
    ├─ BrokenPipe/ConnectionReset (client gone — silent log)
    └─ Unexpected exceptions (streamed: SSE "error" event в родном формате входа —
       _write_sse_error_native; non-streamed: JSON error; после ретраев/исчерпания —
       504/код/502 + файл .err, см. §8.4)
```

### 4.3 GET /v1/models (do_GET, server.py:131–146)

Returns `_AVAILABLE_MODELS` in OpenAI `list` format:
```json
{"object": "list", "data": [<model dict>, ...]}
```

Returns 501 if models haven't been probed yet.

---

## 5. Format conversion

### 5.1 Anthropic → OpenAI (`convert.py`)

| Anthropic field | OpenAI field |
|---|---|
| `system` (string or content blocks) | `messages[0].role = "system"` (concatenated text) |
| `messages[].role = "user"` text blocks | `{"role": "user", "content": "..."}` |
| `messages[].role = "user"` tool_result blocks | `{"role": "tool", "tool_call_id": "...", "content": "..."}` |
| `messages[].role = "assistant"` text | `{"role": "assistant", "content": "..."}` |
| `messages[].role = "assistant"` tool_use | `{"role": "assistant", "tool_calls": [{"id", "type": "function", "function": {"name", "arguments"}}]}` |
| `tools[]` (input_schema) | `tools[]` (type: "function", function.parameters) |
| `tool_choice` | `tool_choice` (auto/required/function) |

### 5.2 OpenAI → Anthropic (non-stream, `convert.py:convert_openai_to_anthropic`)

| OpenAI field | Anthropic field |
|---|---|
| `choices[0].message.content` | `content[].type = "text"` |
| `choices[0].message.tool_calls[]` | `content[].type = "tool_use"` |
| `choices[0].message.reasoning_content` | trace event only (not a content block in this protocol) |
| `choices[0].finish_reason = "tool_calls"` | `stop_reason = "tool_use"` |
| `usage.prompt_tokens` | `usage.input_tokens` |
| `usage.completion_tokens` | `usage.output_tokens` |

**Text fallback**: if response has text but no tool_calls, `parse_tool_calls_from_text()` attempts to extract `<tool_call>...</tool_call>` blocks (Qwen format).

### 5.3 Streaming conversion (`streaming.py:stream_openai_to_anthropic`)

Reads SSE lines from OpenAI backend chunk-by-chunk and emits Anthropic SSE events in real time:

| OpenAI SSE event | Anthropic SSE event |
|---|---|
| `data: {"choices":[{"delta":{"content":"..."}}]}` | `content_block_start` + `content_block_delta` (text) |
| `data: {"choices":[{"delta":{"tool_calls":[...]}}]}` | `content_block_start` + `content_block_delta` (tool_use, partial JSON) |
| `data: {"choices":[{"finish_reason":"stop"}]}` | `message_delta` (stop_reason) + `message_stop` |
| `data: {"usage":{"prompt_tokens":N,"completion_tokens":M}}` | merged into `message_delta.usage` |

Accumulates text, reasoning_content, and tool_calls buffers across chunks, then emits aggregate trace/log events at stream end.

**Usage fix**: requests `stream_options.include_usage=true` from backend (if `ADAPTER_STREAM_INCLUDE_USAGE=1`). If backend doesn't return usage, falls back to heuristic `chars // 4` and marks as estimated in trace.

### 5.4 SSE passthrough-релей E→E (`streaming.py:relay_sse`, v0.9.0)

Для passthrough-маршрутов (входной формат == целевому, см. §4.2) потоковый
ответ бэкенда **не конвертируется**, а релеится клиенту **дословно**:
`relay_sse(resp, wfile, req_id, inp_fmt)` читает ответ построчно и пишет
байты в wfile как есть + flush — без пере-фрейминга и пере-сериализации
(клиент получает ровно тот поток, что прислал бэкенд, в родном формате
входа). Конец потока — по EOF ответа бэкенда (адаптер уже отдал
`Connection: close` в `_start_sse`, поэтому клиент понимает конец и без
терминального события).

Побочно `relay_sse` сканирует проходящие `data:`-строки на **usage-блоки**
(для учёта WEBUI-таблицы «Models in use», §6.6) и возвращает usage
ПОСЛЕДНЕГО встреченного usage-события (или `{}`), нормализованный в
`input_tokens`/`output_tokens` по формату входа (`_USAGE_KEYS`): у
completions usage — на верхнем уровне финального чанка
(`stream_options.include_usage`), у messages — событие `message_delta`, у
responses — во вложенном `response.usage` события `response.completed`
(`_extract_usage`). Обрыв соединения (BrokenPipe/ConnectionReset/
ConnectionAborted) пробрасывается наружу — вызывающий код (do_POST) решает:
CLIENT_GONE или SSE-событие ошибки. Сбой бэкенда **после** старта потока
(заголовки уже ушли) сообщается SSE-событием ошибки в РОДНОМ формате входа
через `_write_sse_error_native` (responses: плоский `{type: "error", code,
message}`; completions: `{"error": {...}}`; messages: антропик-обвязка
`{type: "error", error: {...}}`) — клиент умеет разбирать его в своём
протоколе; запись глотает исключения (клиент мог уже отвалиться).

---

## 6. Backend routing

### 6.1 Configuration

Путь к YAML-файлу бэкендов — `ADAPTER_BACKEND_CONFIG` (структура `backend:`, список записей `name`/`base`/`key`). Парсер (`_parse_backend_yaml`) поддерживает:

```yaml
backend:
  - name: home
    base: "http://127.0.0.1:8002"
    key: ADAPTER_HOME_KEY    # env var name → resolved at parse time
  - name: litellm
    base: "https://llm.example.com"
    key: ADAPTER_LITELLM_KEY
    probe:                   # необязательно — модель на эндпоинт дымовой пробы
      - completions: qwen3.6
      - messages:
      - responses: gpt-5-sol
      - embeddings: text-embedding-3-small
```

### 6.4 Дымовая проба API-эндпойнтов (`probe_endpoints`, config.py)

Статус-страница WEBUI `/` показывает не только список моделей бэкенда, но и
какие известные API-эндпойнты он реально обслуживает. Определение — короткими
POST-запросами с `max_tokens:1` по фиксированному списку `ENDPOINT_PROBES`
(`/v1/chat/completions`, `/v1/messages`, `/v1/responses`, `/v1/embeddings`);
классификация по HTTP-коду: `200` — работает (зелёный ✓ на странице),
любой не-200 код (`400/401/405/429`, `404`, прочие 4xx/5xx) — не работает
(на странице не показывается), сеть/таймаут — ошибка бэкенда.
**Политика «только явно указанные»** (единый конфиг-источник для фоновой
проверки бэкендов, usage-пробы модели и usage-reprobe): пробуется ТОЛЬКО
эндпоинт, перечисленный в ключе `probe` YAML-записи бэкенда **с непустой
моделью** (у разных эндпоинтов бэкенда свои probe-модели). Ключа `probe` нет,
эндпоинт не перечислен или значение пустое — проба этого эндпоинта **НЕ
выполняется** (молчаливый пропуск, не ошибка; состояние бэкенда в
`_ENDPOINT_STATE` для неперечисленных путей не создаётся). Заданная в `probe`
модель, отсутствующая среди моделей бэкенда, пропускает только свой эндпоинт
(точечный skip с текстом в `errors`); «дефолтной модели» больше нет — только
явно указанные в `probe` (см. ADR v0.8.5). Бэкенд без `probe` ошибкой не
считается: ему пробы не нужны, колонка «Доступные API» остаётся пустой.

Проба встроена в `config.refresh_models` (конец функции, после обновления кэша
моделей): вызывается при каждой фоновой проверке бэкендов — при старте
адаптера, на первом GET `/` и по кнопке-иконке 🔃 «Перепроверить бэкенды» (POST `/` →
`config.start_refresh` → фоновый воркер, см. §6.5). Результат добавляется в
возвращаемый dict ключом `"probe"` (старые читатели `ok/count/errors` не
ломаются) и кэшируется в `_ENDPOINT_STATE` (~60 с, `ENDPOINT_PROBE_TTL`);
повторная проверка в пределах TTL сеть не трогает. **`_ENDPOINT_STATE` —
единый источник** для колонки «Доступные API» бэкенда и экспортёра: сюда же
`model_usage` переносит found-эндпоинты дымовых проб моделей (первое
обращение, usage-reprobe, загрузка `model-usage.yaml` — см. §6.6;
`config.upsert_endpoint_state`). Мастер-флаг `ADAPTER_ENDPOINT_PROBE=0`
отключает автопробу бэкендов.
Фактическая проба пишется в консоль блоком `[ENDPOINT_PROBE]` (print, гейт
`ADAPTER_DEBUG_ENABLE`) — одна строка на бэкенд с сырыми HTTP-кодами; кэш-хиты
не логируются. Проба чисто наблюдательная: на маршрутизацию запросов не влияет.

### 6.5 Фоновая проверка бэкендов (start_refresh / refresh_state, config.py)

Раньше каждый GET/POST статус-страницы `/` синхронно гонял
`config.refresh_models` (опрос `/v1/models` всех бэкендов + дымовая проба), и
при недоступном/медленном бэкенде HTTP-ответ висел (N бэкендов × 10 с на
эндпоинт). Теперь проверка — **фоновая**, запускается при старте адаптера, на первом
GET `/` и по кнопке:

- **Состояние** — модульный снимок-словарь `_REFRESH_JOB` в `config.py`:
  `running`, `started_at`/`done_at` (time.time), `ok`/`count`/`providers`/
  `errors` (итог последней `refresh_models`; `providers` — число настроенных
  бэкендов на момент финальной публикации), `checked_at` («HH:MM:SS»
  завершения). Снимок
  **иммутабелен** — заменяется целиком (атомарная замена ссылки), читатели
  (`webui_status`) берут `refresh_state()` без лока. До первой проверки —
  дефолт-словарь (все None/False).
- **Запуск** — `start_refresh(timeout, reload=True)`: при старте адаптера
  (`backend-adapter.py` после поднятия WEBUI), на первом GET `/`
  (автостарт, `webui_status._autostart_first_check`) и по кнопке
  🔃 «Перепроверить бэкенды» (POST `/`). При `reload=True` (по умолчанию) перед
  запуском воркера вызывается `config.reload_backend_config()` — кнопка
  **перечитывает** `ADAPTER_BACKEND_CONFIG` на лету: `_BACKENDS`/
  `_BACKEND_BY_NAME`/`_DEFAULT_BACKEND` подменяются новым списком, из
  `_ENDPOINT_STATE` удаляются записи бэкендов, которых больше нет в YAML
  (stale-очистка), кэши моделей/индексы не трогаются — их пересоберёт
  `refresh_models` по новым бэкендам. Битый/недоступный YAML — `reload_
  backend_config()` возвращает `None`, прежние бэкенды остаются, `[WARN]`;
  фоновая проверка всё равно перепроверяет прежних. Под `_REFRESH_LOCK`
  публикует `running=True` и стартует daemon-поток `_refresh_worker`; пока
  проверка идёт, повторный вызов возвращает `False` (второй поток не
  создаётся). `_refresh_worker` зовёт `refresh_models(timeout)` и в `finally`
  публикует финальный снимок (`running=False`, результат или текст исключения
  в `errors["__worker__"]`) — проверка не может «зависнуть навсегда».
  HTTP-ответ не блокируется.
- **Страница** (`webui_status.py`): GET `/` читает `config.refresh_state()` и
  рендерит последний результат; если проверок ещё не было (done_at пуст) и
  есть что проверять — первый заход сам запускает ПЕРВУЮ проверку
  (`_autostart_first_check`), повторные — только по кнопке. POST `/`
  (кнопка 🔃 «Перепроверить бэкенды») вызывает `start_refresh(timeout=
  PROBE_TIMEOUT)` и отвечает **303 See Other** на GET `/` (PRG: браузер
  переходит на страницу GET-навигацией, авто-релоад не повторяет POST).
  Футер проверки — «Список провайдеров обновлён в HH:MM:SS (N провайдеров,
  M моделей).» (N — `providers` снимка после перечитывания, M — моделей в
  кэше); при провале — «Не удалось обновить список провайдеров…».
  Пока проверка идёт, страница показывает баннер «Проверка выполняется…»
  и JS `status_poll` опрашивает JSON-эндпоинт **`/api/refresh-state`**
  (`RefreshStateEndpoint`, тот же модуль) каждые ~2 с; как только
  `running=false` и есть `done_at` — `location.reload()` рендерит свежий
  результат. Авто-релоад безопасен: он происходит на GET-документе,
  повторного POST нет, зацикливания нет.
- `refresh_models` при этом мутирует конфиг-глобалы (`_AVAILABLE_MODELS` и
  др.) из фонового потока — то же отношение, что было при синхронном
  refresh; отдельный Lock вокруг внутренностей не добавляется (тот же
  компромисс, что и раньше — см. комментарий у `_refresh_worker` в
  config.py), менеджер синхронизирует только запуск и публикацию результата.

### 6.6 Таблица использованных моделей («Models in use», `model_usage.py`)

Отдельный модуль-лист DAG (импортирует только `config` и `yaml`; его
импортируют `server.py` и `webui_status.py` — цикла нет, `config.py`
остаётся корнем). Ведёт **персистентную таблицу** клиентских моделей, к
которым агент реально обращался, и для каждой — доступность 4 известных
API-эндпоинтов бэкенда **именно этой моделью**. Таблица сохраняется в
YAML-файл `model-usage.yaml` в корне WEBUI (см. ниже, «Персистентность»)
и переживает перезапуски адаптера:

- **Точка учёта** — `server.do_POST`, сразу после strict-проверки модели
  (клиентское имя из BODY, **до** маппинга `_MAP`): `model_usage.record_
  model_usage(client_model)`. Недопустимая модель (HTTP 400) в таблицу не
  попадает — хук стоит после `return`; провал резолва/пробы никогда не
  роняет запрос (все исключения ловятся внутри).
- **Схема строки** (`client_model` — ключ `_TABLE`): `backend` (имя из
  `_resolve_backend`), `calls` (счётчик обращений, растёт всегда),
  `input_tokens`/`output_tokens` (токены из usage-блоков ответов бэкенда,
  см. ниже), `endpoints` (`{pname: {status, found}}` — только реально
  пробованные пути; `found` ⇔ HTTP 200), `errors` (тексты сетевых ошибок
  пробы), `first_seen` («HH:MM:SS»), `probing` (True, пока первый запрос
  выполняет синхронную пробу). Колонки WEBUI-секции идут в порядке
  `config.ENDPOINT_PROBES` (completions/messages/responses/embeddings).
- **Поток первого обращения** — короткая критическая секция под
  `_TABLE_LOCK` (только поиск/создание/инкремент), затем **вне лока**:
  резолв бэкенда (`config._resolve_backend`, без сети — имя для колонки) и,
  если `config.ADAPTER_MODEL_USAGE_ENABLE`, синхронная дымовая проба
  `_probe_model_endpoints(backend_cfg, resolved)`. Какие эндпоинты пробовать —
  политика «только явно указанные» из §6.4: только pname, перечисленные в
  `probe` YAML-записи бэкенда с непустой моделью (неперечисленные/пустые —
  молчаливый пропуск); «кем пробовать» — resolved-имя ЗАПРОСА (не
  probe-модель бэкенда). Та же низкоуровневая `config._probe_backend_endpoints`,
  что у фоновой проверки бэкендов (§6.4), но без TTL-кэша: результат живёт
  в строке таблицы, а найденные (HTTP 200) эндпоинты **синхронизируются** в
  `config._ENDPOINT_STATE` бэкенда через `config.upsert_endpoint_state` —
  единый источник колонки «Доступные API» и экспортёра (§6.4) видит их.
  Таймаут одного POST — `MODEL_USAGE_PROBE_TIMEOUT = 10.0` (первый запрос
  новой модели ждёт до 4×10 с; осознанно, см. ADR). Если resolved-модели
  нет среди моделей бэкенда в `_MODEL_TO_BACKEND` — проба не выполняется
  (колонки эндпоинтов «—»).
- **Повторные обращения** — строка уже есть → только `calls += 1`, проба
  никогда не повторяется. Конкурентность: два одновременных первых
  обращения к одной модели дают одну пробу (вторая нить видит строку),
  к разным — пробы идут параллельно в потоках `ThreadingHTTPServer`.
- **Счётчики токенов usage** — `input_tokens`/`output_tokens` копятся в
  локальных переменных `do_POST` из **usage-блоков ответов бэкенда** и
  фиксируются ОДИН раз в существующем `finally` через
  `model_usage.add_usage_tokens(client_model, input, output)` (единая точка
  на любой исход — успех, ошибка бэкенда, исчерпание ретраев, исключение).
  Источники: non-stream — тело ответа после `convert_openai_to_anthropic`
  (`usage.prompt_tokens` → input, `usage.completion_tokens` → output);
  stream — `(stop_reason, usage)` из `stream_openai_to_anthropic` (последний
  chunk.usage). Успешная попытка **без usage** токенов не даёт (0) — реальные
  токены запроса заранее неизвестны, их сообщает только usage ответа (счёт
  байтов тел и параметр `bytes_sink`, дававшие только объём обмена, удалены);
  эвристика `chars/4` из streaming.py остаётся только для trace-поля
  `input_tokens_estimated` клиента и в учёт не попадает. Гейт — тот же
  `ADAPTER_MODEL_USAGE_ENABLE`; запросы, не прошедшие strict-проверку
  (HTTP 400), и служебные дымовые пробы эндпоинтов токенов не дают.
- **Персистентность** — таблица сохраняется в YAML-файл `model-usage.yaml`
  в корне WEBUI (= `ADAPTER_DEBUG_LOGPATH`, дефолт `./tmp/logs`); файл
  несёт версию формата (`version: 2`, строки — под ключом `models`). Точка
  синхронизации пути — `webserver.serve()` (`set_persist_path(root_dir)`;
  в standalone — явный `[ROOT]`). Загрузка — ленивая, при первом обращении
  к пустой таблице (`_ensure_loaded_locked` под `_TABLE_LOCK`): строки
  нормализуются (`probing` всегда False, счётчики/токены — неотрицательные
  int, незнакомые pname/ключи отбрасываются); битый файл/незнакомая версия
  (> 2) игнорируются (таблица стартует пустой). **Файл `version: 1`**
  (учёт в байтах) при загрузке НЕ игнорируется — миграция:
  `calls`/`endpoints`/`errors`/`first_seen` сохраняются, байтовые поля
  (`bytes_sent`/`bytes_recv`) отбрасываются (`_normalize_row` не находит их
  в схеме), токены стартуют с 0; следующие сохранения пишут `version: 2`.
  Сохранение «грязной» таблицы — не чаще раза в
  `config.ADAPTER_MODEL_USAGE_SAVE_INTERVAL` (сек, дефолт 300); создание
  строки, обнуление счётчиков строки (`reset_model`: calls/input_tokens/
  output_tokens → 0, строка НЕ удаляется), удаление строки (`delete_model`:
  `del _TABLE[model]` под `_TABLE_LOCK` + `_DIRTY=True`, файл
  перезаписывается без строки — в отличие от reset, строка уходит и из
  памяти, и из YAML), завершение перепроверки
  (`_save_table(force=True)`) и завершение работы (`flush_table`, в т.ч.
  Ctrl-C) сохраняют сразу. Строки с `probing: True` на диск не попадают;
  запись атомарная (tmp + `os.replace`). Загруженные строки не
  перепроверяются — fast-path на повторных обращениях; «освежить» результаты
  проб строки без сброса счётчиков — фоновая перепроверка
  (`POST /api/model-usage/reprobe`, см. ниже). При загрузке found-эндпоинты
  строк (для бэкендов из текущего `_BACKENDS`) синхронизируются в
  `config._ENDPOINT_STATE` через `config.upsert_endpoint_state` — колонка
  «Доступные API» бэкенда и экспортёр видят их сразу, без сети (контракт
  «загруженные строки не перепробуются» сохраняется: синхронизация кэша —
  не проба).
- **Мастер-флаг `ADAPTER_MODEL_USAGE_ENABLE`** (config.py, дефолт `1`):
  `0` — пробы и накопление токенов отключены, учёт обращений остаётся
  (колонки эндпоинтов «—», Input/Output — «0»). Пробы по модели НЕ зависят
  от `ADAPTER_ENDPOINT_PROBE` (тот управляет только фоновой проверкой
  бэкендов §6.4/§6.5). В runtime-пул `/config` флаг не входит;
  персистентность работает независимо от мастер-флага.
- **Вывод** — секция «Models in use» на статус-странице `/` сразу под
  кнопкой проверки бэкендов 🔃 (подписи-абзаца перед ней нет; футер о
  проверке и кнопка — под таблицей бэкендов), рендер —
  `webui_status._usage_rows_html`, `model_usage.usage_snapshot()` — копии
  строк в порядке первого обращения; первый вызов после старта загружает
  таблицу из YAML. Колонки: Модель | Бэкенд | Вызовов | Input | Output |
  Cost | **Endpoints** | Actions (8). Endpoints — одна колонка: только доступные
  эндпоинты строки короткими именами через запятую (зелёным, порядок
  `config.ENDPOINT_PROBES`; ничего доступного — серая «—»). Токеновые
  колонки рендерятся форматтером `_fmt_tokens` (точное число с неразрывным
  пробелом-разделителем тысяч: «12 345»; «0» — usage в ответах не было).
  **Live-счётчики**: JS `usage_poll` (безусловный, в <head>) каждые ~5 с
  опрашивает GET `/api/model-usage/snapshot` (`UsageSnapshotEndpoint` →
  `usage_snapshot()`, из памяти, сети к бэкендам нет) и обновляет только
  ячейки Вызовов/Input/Output (data-атрибуты на td; позиционный матчинг со
  снимком); число строк изменилось (строка удалена/новая модель) —
  `location.reload()`.
- **Перепроверка строки (reprobe)** — `POST /api/model-usage/reprobe?model=
  <имя>` (`ModelUsageReprobeEndpoint`): повторная дымовая проба эндпоинтов
  **именно этой моделью** (по политике «только явно указанные» из §6.4 —
  только pname из `probe` бэкенда с непустой моделью) для строк с колонками
  «—» (первый запрос давно / бэкенд ожил / модель появилась в `/v1/models`).
  Запуск — в фоне:
  `model_usage.start_reprobe` публикует снимок `_REPROBE` (client_model →
  `started_at`) под `_REPROBE_LOCK` (отдельный от `_TABLE` и от
  `config._REFRESH_JOB`; НЕ ставит `probing` строки — перепроверка не
  выкидывает строку из сериализации) и стартует daemon-поток `_reprobe_worker`
  → `reprobe_model(client_model)` (ядро: строка есть и не `probing` →
  резолв + `_backend_has_model` + `_probe_model_endpoints` с таймаутом
  `MODEL_USAGE_PROBE_TIMEOUT`; обновляет только `backend`/`endpoints`/
  `errors`, `_DIRTY = True` + `_save_table(force=True)`; **calls и токены не
  трогает** — проба служебная, не обращение агента; найденные эндпоинты
  синхронизируются в `config._ENDPOINT_STATE` (§6.4); исключения ловятся).
  Не-JSON (кнопки-иконки в колонке Actions: ⟳ «Перепроверить» / ↺
  «Сбросить» / ✕ «Удалить» — три отдельные формы в одной ячейке,
  `_actions_cell_html` по `_ACTIONS`; тексты-фразы в `title`/`aria-label`)
  — 303 See Other на GET `/` (PRG); JSON — 202
  «запущено», 404 «нет строки / уже идёт / первая проба ещё выполняется»,
  400 «нет model». Пока перепроверка идёт: баннер «Перепроверка модели X…»,
  в строке — серый «проверяется…» вместо кнопок; JS `reprobe_poll`
  опрашивает GET `/api/model-usage/reprobe-state` (`ReprobeStateEndpoint`,
  JSON из `model_usage.reprobe_state()`: running/model/started_at) каждые
  ~2 с и делает `location.reload()` по завершении — состояние живёт в
  `model_usage`, не в `config.refresh_state()`.
- **Удаление строки (delete)** — `POST /api/model-usage/delete?model=<имя>`
  (`ModelUsageDeleteEndpoint`, по образцу reset): `model_usage.delete_model`
  — под `_TABLE_LOCK`: `_ensure_loaded_locked()` + `del _TABLE[model]` +
  `_DIRTY=True`; вне лока `_save_table(force=True)` (файл сразу без строки).
  Чистка `config._ENDPOINT_STATE` НЕ требуется: кэш доступности эндпоинтов —
  общий на бэкенд, не по моделям (стирание было бы гонкой с фоновой пробой).
  Безопасен при идущей перепроверке строки (`reprobe_model` в finally
  увидит `row is None` и не мутирует). Не-JSON (кнопка ✕) — 303 на GET `/`
  (PRG); JSON — 200 `{"ok": true, "model": ...}` при удалении, 404 «строки
  нет» (повторное удаление — строка уже ушла; delete НЕ идемпотентен, в
  отличие от reset), 400 «нет model». GET на префикс — 404. Сброс/удаление
  не совмещены: reset обнуляет счётчики и сохраняет строку, delete убирает
  строку целиком.

### 6.7 Health-эндпоинты (`webui_ops.py`, `/healthz` `/health` `/live` `/ready`)

На общем WEBUI-слушателе (НЕ на порту адаптера и не на порту экспортёра)
живут эндпоинты оркестрации — модуль `webui_ops.py`
(`@webserver.register`, импортируется в `webserver.serve()` вместе с
`webui_status`/`webui_config_api`). Назначение и ответы:

- **`HealthzEndpoint`** (prefix `/healthz`) и алиас **`HealthEndpoint`**
  (prefix `/health`) — идентичны: GET без remainder → 200 `application/json`
  `{"status": "ok", "version": <версия из WebContext.version>,
  "uptime": <сек с импорта модуля, модульная константа _BOOT_TS>,
  "pid": <os.getpid()>}` (общий хелпер `_health_body(context)`).
- **`LiveEndpoint`** (prefix `/live`) — liveness: процесс жив и сервер
  отвечает (тот же JSON, 200).
- **`ReadyEndpoint`** (prefix `/ready`) — readiness: 200
  `{"status": "ready", ...}`, если `config._BACKENDS` непуст И
  `config._AVAILABLE_MODELS` непуст (стартовый/последний опрос моделей
  прошёл — «бэкенды настроены и прогреты»); иначе **503**
  `{"status": "not_ready", "reason": "..."}` (нет бэкендов / кэш моделей
  пуст). Тело и Content-Type отдаются через `handler._write` (send_error не
  используется).

Все — только GET: непустой `remainder` → 404; POST — 405 дефолтом базового
класса. Конфликт префиксов нет: матчинг по самому длинному префиксу —
`/health` не матчит `/healthz`. Эндпоинты чисто наблюдательные: на
проксирование и проверки не влияют.

### 6.8 Prometheus-экспортёр (`prometheus_exporter.py`, `ADAPTER_EXPORTER_PORT`)

Отдельный лёгкий HTTP-слушатель на `ADAPTER_EXPORTER_PORT` (дефолт **9100**)
и адресе `ADAPTER_WEBUI_HOST`, поднимается вместе с WEBUI при
`ADAPTER_EXPORTER_ENABLE=1` (backend-adapter.py, блок WEBUI). **НЕ эндпоинт
общего WEBUI-ядра**: повторный `serve()` перезаписал бы
`Handler.context/endpoints` и открыл бы весь WEBUI на порту экспортёра, а
`/metrics` не должен висеть на порту статуса. Роутинг минимален — только
`/` и `/metrics` (200 text/plain), прочее — 404; `log_message` подавлен
(тишина, как QuietWebServer); stdlib-only, клиентских библиотек Prometheus нет.

- **Источники — живые конфиг-глобалы** (те же, что у страницы `/`):
  настройки/статус приложения (`MetricsHandler.version` — классовый атрибут,
  выставляется `serve_exporter(version, ...)` из `__version__`; `_BOOT_TS` —
  модульная константа uptime, как в webui_ops), число бэкендов
  (`config._BACKENDS`), моделей в кэше (`config._AVAILABLE_MODELS`),
  refresh-ошибки (`config.refresh_state()`);
- **По бэкенду** (label `name`, `base`): `backend_adapter_backend_up`
  (1/0 — есть ли имя бэкенда в `errors` снимка последней проверки),
  `backend_adapter_backend_models` (число моделей бэкенда в
  `_MODEL_TO_BACKEND`), `backend_adapter_backend_endpoint{endpoint=pname}` —
  по `config._ENDPOINT_STATE` (единый источник после синхронизации §6.4/§6.6);
- **По использованной модели** (label `model`, `backend`):
  `backend_adapter_model_calls_total`, `backend_adapter_model_input_tokens_total`,
  `backend_adapter_model_output_tokens_total` из `model_usage.usage_snapshot()`
  (уже копирует таблицу под `_TABLE_LOCK`);
- **text exposition 0.0.4 без библиотек** — `# HELP`/`# TYPE` на группу,
  gauge/counter; label-значения (имена бэкендов/моделей) экранируются
  (`\\`, `"`, перевод строки). Чтение глобалов — напрямую, как рендер
  страницы; экспортёр не должен ронять адаптер: OSError на bind → `None`
  + строка `[EXPORTER] Failed to bind ...`, остальное работает.

### 6.9 JSON-результаты проверок в LOGPATH (`probe_json.py`, v0.9.0)

Безусловный наблюдательный канал (как .err-файлы, см. §8): результаты
проверок бэкендов пишутся плоскими JSON-файлами в корень
`ADAPTER_DEBUG_LOGPATH`, рядом с `model-usage.yaml` и корнем WEBUI:

- `<имя_бэкенда>.models.json` — проверка бэкенда на доступные модели
  (`.models.json`): стартовый `_init_multi_backends` (config.py), фоновая
  `refresh_models` (кнопка «⟳ Перепроверить»), reload-перечитывания;
- `<имя_бэкенда>.<конверт.модель>.<pname>.json` — проверка эндпоинта модели
  (config.py `probe_endpoints` — фоновая проба бэкенда; model_usage.py
  `_probe_model_endpoints` — пер-модельные пробы usage-таблицы: первое
  обращение модели и reprobe строки). `pname` — из `ENDPOINT_PROBES`:
  `completions | messages | responses | embeddings`.

Конвертация имени модели для файла: все `/` и `:` → `_`
(`org/model:v1` → `org_model_v1`); остальные символы не трогаются.
Каждый файл при каждой новой проверке ПЕРЕЗАПИСЫВАЕТСЯ целиком (атомарно:
tmp + `os.replace`), .tmp-хвостов не остаётся. Канал не гейтится
`ADAPTER_DEBUG_ENABLE` / `ADAPTER_DEBUG_PARTS` / `ADAPTER_DEBUG_TRIM`;
директория создаётся при записи. Секреты маскируются `redact()` по
умолчанию (полные данные — при `ADAPTER_SENSITIVE_LOGGING_ENABLE=1`,
живое чтение config); любая ошибка записи молча глотается — проверку не
роняет. Модуль — лист DAG: импортируется config.py и model_usage.py
(оба корня DAG), сам на верхнем уровне — stdlib only.

### 6.10 Таблица активных сессий агентов («Sessions», `session_registry.py`, v0.9.2)

In-memory реестр (`_TABLE: dict[SessionKey → строка]` + lock), где
**`SessionKey = tuple[str, str, str, str, str]` = `(session, agent, model,
backend, route)`**, а строка — «закреплённое соответствие» агента, модели,
бэкенда и обработчика в рамках клиентской сессии (заголовок
`X-Claude-Code-Session-Id`), плюс сопровождающие поля: время последнего
обращения, счётчики обращений и ошибок. Смена модели агентом или смена
обработчика (правило TARGET) — **новое событие → новая строка**; возврат к
уже встречавшемуся кортежу — та же строка (upsert, `calls++`). Секция
«Sessions» на статус-странице `/` — `webui_status._sessions_rows_html`;
live-обновление — JS `sessions_poll` → `/api/sessions/snapshot` (см.
`docs/webui.md` §3/§6).

Точки учёта — `server.do_POST` (все через хелпер `_register_session` →
`session_registry.register`, возвращающий ключ в thread-local
`_req_ctx.session_key`):

- **400-ветки** («Invalid JSON», «Missing model», strict-валидация) —
  `register` с **пустыми** `model/backend/route`: до `routing.decide` не
  дошли. Пустой кортеж самодокументируется и не «сливается» с полной строкой
  реальной модели (иного дефекта);
- **после `routing.decide`** — `register(session, agent, model=client_model,
  backend=backend_name, route=route_str)`, ДО ветки disabled/reject (таблица
  показывает, куда агент пытался: `route` = `passthrough messages→messages` /
  `convert messages→completions` / `reject` / `disabled`);
- 404 на не-входной путь строку **не** создаёт — учёт стоит после `return`
  (исключение между распознаванием пути и `register` тоже строки не создаёт);
- `record_error(key)` — из общих `_send_json`/`_send_raw` по финальному
  статусу ≥ 400 (покрывает все пути ошибок без правки ~20 точек вызова),
  ключ берётся из `_req_ctx.session_key`. Для `None`/неизвестного ключа —
  no-op, поэтому служебные ответы (404 не-входных путей, GET `/api/*`,
  health) в счётчик не попадают.

Строки сортируются по `_ts` desc (новые сверху; строка всплывает при новом
обращении). Глубина — живой лимит `config.ADAPTER_SESSIONS_TABLE` (дефолт 10;
0 — таблица отключена): лимит считает **строки (кортежи)**, а не сессии, —
при регистрации обращения самая старая строка вытесняется. Снимок отдаёт
поле `key` — компактную JSON-строку кортежа (`key_json`), единый
дискриминатор строки для JS (та же строка в `data-key` HTML), т.к. `session`
не уникален. **Персистентности нет** (осознанное решение): таблица живёт
только в памяти процесса, каждый запуск начинается заново. Модуль — лист DAG:
на верхнем уровне импортирует только config (читает `ADAPTER_SESSIONS_TABLE`
живьём — переживает reload конфига в тестах); потребители — server.py (пишет)
и webui_status.py (читает/рендерит).

### 6.2 Разрешение коллизий имён моделей

Когда одна и та же модель встречается на нескольких бэкендах, генерируется префиксный ID:

```
<backend_name>.<model_id>  →  e.g.  "home.qwen3.6-35b-a3b"
```

Префикс получают только конфликтующие ID; уникальные ID проходят без изменений.

### 6.3 Логика маршрутизации (`_resolve_backend`, config.py:435–475)

12. **Явный префикс** — снять префикс `<backend_name>.`, направить на соответствующий бэкенд
13. **Lookup по списку моделей** — поиск в `_MODEL_TO_BACKEND`
14. **Fallback** — первый бэкенд в конфиге (`_DEFAULT_BACKEND`); если бэкенд не сконфигурирован/не найден — `RuntimeError`

---

## 7. Tool-use causality tracking

Claude Code's agent loop can send **parallel requests** within a single session (e.g., main agent turn + structured_output sidebar). Reconstructing "which request produced tool_use X, which request returned its tool_result" from timestamps alone is unreliable.

Solution: `tool_use_id` is the natural unique key.

15. **Register** (`_register_tool_use`): when converting OpenAI → Anthropic response, record `(session_id, tool_use_id, req_id)` in an `OrderedDict` per session
16. **Lookup** (`_lookup_tool_use_producer`): when processing incoming `tool_result`, find the `req_id` that produced the corresponding `tool_use`
17. **Eviction**: FIFO eviction at `_TOOL_USE_INDEX_MAX_PER_SESSION` (2000 entries) to bound memory for long sessions

Trace event `tool_result` includes `parent_req_id` — `null` if the producer was evicted or never recorded.

---

## 8. Observability stack

### 8.1 Debug logging (`logger.py`)

`_d(msg)` / `_dr(req_id, msg)` — timestamped console+file debug log (`_write`)

- **Console: unconditional** (always printed — v0.8.6), **trimmed** to
  `ADAPTER_DEBUG_TRIM` chars (`0` = no trim)
- **File**: per-session `session-<ts>-<sid>.log` in `ADAPTER_DEBUG_LOGPATH`,
  written **only** when `ADAPTER_DEBUG_ENABLE=1` — and carrying the **FULL
  line, untrimmed**: the file channel is inherently full-part (v0.8.6 reform)
- All messages pass through `redact()` to mask secrets (unless `ADAPTER_SENSITIVE_LOGGING_ENABLE=1`)

The runtime flags below are **runtime-switchable**: the WEBUI endpoint
`/config` re-reads them live (see §8.6) instead of restarting the adapter.

Debug flags (runtime pool):

| Flag | Purpose |
|---|---|
| `ADAPTER_DEBUG` | File logging master switch (full parts, no trim) |
| `ADAPTER_DEBUG_PARTS` | Per-session `.json`+`.yaml` dumps of all logged protocol parts |
| `ADAPTER_DEBUG_TRIM` | Console trim limit in chars (0 = no trim; default 3000) |
| `ADAPTER_SENSITIVE_LOGGING_ENABLE` | Disables the redaction sanitizer (1 = raw secrets) |

### 8.2 Structured trace (`tracer.py`)

JSONL format per event:

```json
{
  "ts": "2026-08-30T12:34:56.789Z",
  "session_id": "...",
  "req_id": "abc123def456",
  "seq": 42,
  "event": "response_content",
  "text_len": 1234,
  "tool_uses": [...],
  "finish_reason_raw": "tool_calls",
  "stop_reason_mapped": "tool_use",
  "reasoning_present": true,
  "reasoning_len": 567,
  "reasoning": "..."
}
```

Event types:

| Event | When |
|---|---|
| `request_start` | Every incoming POST |
| `model_map` | Model mapping applied |
| `backend_attempt` | Each retry attempt |
| `backend_result` | Success or error from backend |
| `response_content` | After non-stream response or stream end |
| `tool_call` | (via tool_uses array in response_content) |
| `tool_result` | Incoming tool_result with causality chain |
| `tool_call_fallback` | JSON extracted from text (not native tool calling) |
| `usage_report` | Token usage (stream mode) |
| `adapter_invariant_check` | System message position validation |

Output:
- Per-session JSONL files (`session-<ts>-<sid>.jsonl`) in the same
  `ADAPTER_DEBUG_LOGPATH` directory as debug logs (only when the path is set —
  empty means file logging off)
- Written only when `ADAPTER_DEBUG_ENABLE=1` (master switch)
- All trace lines redacted (unless `ADAPTER_SENSITIVE_LOGGING_ENABLE=1`)

### 8.3 Secret redaction (`redact.py`)

Regex-based masking applied to all log output:

| Pattern | Masking |
|---|---|
| `Bearer <token>` | `Bearer abcd***REDACTED****@xyz` |
| `VAR_NAME = <secret>` | `VAR_NAME = abcd***REDACTED****@xyz` |
| Pattern suffixes: `_PAT`, `_KEY`, `_TOKEN`, `_SECRET`, `API_KEY` | |

Long base64/hex strings may also be matched.

### 8.4 Per-session files (`session_log.py`)

- File naming: `session-<YYYYMMDD-HHMMSS>-<sessionID_short>.<ext>` (`.log` — debug,
  `.jsonl` — trace), plus `session-*.parts/` dump directories — all flat in
  `ADAPTER_DEBUG_LOGPATH`
- Timestamp frozen on first use per session (all traffic → same file)
- FIFO eviction at `_LOG_FILES_PER_SESSION` (5000 entries)
- The log directory `ADAPTER_DEBUG_LOGPATH` is created on demand when set
  (adapter startup when `ADAPTER_DEBUG_ENABLE=1`, and/or at first write);
  empty (default) — no directory, no disk writes

### 8.5 Per-session protocol dumps — `ADAPTER_DEBUG_PARTS` (`.json` + `.yaml` pairs)

`ADAPTER_DEBUG_PARTS=1` (plus the master switch `ADAPTER_DEBUG_ENABLE=1`) enables
per-session dumps of **all** logged protocol parts — there is no fixed tag list
anymore (the old `ADAPTER_DEBUG_TAGS_FULL` / `ADAPTER_DEBUG_TAGS_OUT_ALL`
selectors were removed in the v0.8.6 logging reform).

- Only activates when `ADAPTER_DEBUG_ENABLE=1` (master switch for file writes) and
  the log directory `ADAPTER_DEBUG_LOGPATH` is set (created on demand)
- Creates `session-<datetime>-<sessid8>.parts/` directory next to the session log files
- Writes a **pair** of files per logged part — `.json` (machine-readable,
  `json.dumps(indent=2)`) and `.yaml` (human-readable) — via
  `session_log.write_debug_json(session_id, tag, data)`; tags: `BODY`,
  `TOOL_RESULT` (one full dict per result, including errors — the separate
  `TOOL_RESULT_ERROR` tag is gone), `OPENAI_BODY`, `FETCH_RAW`, `RESPONSE`
  (stream: aggregated snapshot). Dumps carry full data, no trimming
- Thread-safe: uses `threading.Lock` on the per-session counter
- Full lines also land in `session-*.log` (`.parts` dumps are the machine-readable
  twin of the debug blocks)

### 8.6 Runtime config pool + WEBUI endpoint `/config` (`webui_config_api.py`)

`config.py` keeps a `RUNTIME_CONFIG_POOL` — variables whose value safely applies
to the *next* call/request (volume of disk writes, log sanitizing, streaming
and strict-models switches), flip-able without restarting the adapter:

| Type | Variables |
|---|---|
| bool | `ADAPTER_DEBUG`, `ADAPTER_DEBUG_PARTS` |
| bool | `ADAPTER_SENSITIVE_LOGGING_ENABLE`, `ADAPTER_STREAMING_ENABLE`, `ADAPTER_STREAM_INCLUDE_USAGE`, `ADAPTER_STRICT_MODELS` |
| int | `ADAPTER_DEBUG_TRIM`, `ADAPTER_TRACE_REASONING_MAX_CHARS`, `ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS` |
| *(none)* | no string members — the old detail selectors were removed (v0.8.6) |

- `get_runtime_config()` — snapshot dict `{name: value}`; `set_runtime_config(**kw)`
  type-validates against `_RUNTIME_CONFIG_TYPES` and silently ignores out-of-pool
  keys / wrong types (bool is checked before int — `bool` subclasses `int`), then
  re-assigns `config` module globals. Returns the post-state dict.
- **Readers must read live** — `config.ADAPTER_X` module attribute at call time,
  not `from .config import X` import-time snapshots. All pool consumers were
  refactored to live reads: `logger.py` (`_write`/`trim_limit`), `server.py`,
  `convert.py`, `streaming.py`, `tracer.py`.
- Deliberately **excluded** from the pool: network, backend config/mapping,
  listen addresses/ports (`ADAPTER_PROXY_PORT`, `ADAPTER_ENDPOINT_HOST`,
  `ADAPTER_WEBUI_*`), timeouts/retries, detach/pidfile, `ADAPTER_DEBUG_LOGPATH`
  (directory identity must not change mid-flight — the session logger would
  write to a moving target).
- Endpoint `webui_config_api.py` (`@webserver.register`, prefix `/config`): GET —
  HTML form (6 bool checkboxes + 3 int inputs, no string fields) with current
  pool values; POST —
  `application/x-www-form-urlencoded` or JSON body → `set_runtime_config()`,
  response re-renders with a flash «Применено»/«Игнорировано» split. Links to
  the form: session tabs panel («Runtime config 🔧») and the status page `/`
  (header icons 📋 → `/session`, 🔧 → `/config`; back links «Статус 📊»).
- The pool keys double as **env vars at startup** — the same names still read from
  the environment by `config.py`; `/config` only overrides the process state.

---

## 9. Streaming architecture

### 9.1 The connection:close lie fix

The adapter runs with default `BaseHTTPRequestHandler.protocol_version` = **HTTP/1.0**. With HTTP/1.0, `http.server` **always** closes the TCP connection after a response (`self.close_connection = True`), regardless of any `Connection` header.

Previously the adapter sent `Connection: keep-alive` while silently closing the connection — clients (Node.js/Stainless Claude Code SDK) trusted the header, pooled the socket, and got `ECONNRESET` on reuse. This manifested as **"API Error: The operation timed out"** in the Claude Code terminal *after* the adapter had successfully processed a request.

Fix: send `Connection: close` and set `self.close_connection = True` explicitly in `_start_sse()`.

### 9.2 ThreadingHTTPServer

Claude Code sends **parallel concurrent requests** (tool calls, structured_output sidebar). A single-threaded `TCPServer` would serialize them — while one request waits `ADAPTER_TIMEOUT` seconds for the backend, others time out on the client side, causing `BrokenPipeError`.

Solution: `ThreadingHTTPServer` — each request handled in its own thread.

`QuietThreadingHTTPServer` suppresses tracebacks for `BrokenPipeError`, `ConnectionResetError`, `ConnectionAbortedError` (client-side disconnects, not adapter errors).

### 9.3 Streaming retry semantics

Streaming retry is **pre-header only**: once `_start_sse()` sends headers and the first `content_block_start` event, the response stream has started. A retry at that point would send a duplicate `message_start`, which the client rejects. If the backend connection fails mid-stream, an SSE `error` event is emitted and the stream ends (no retry).

### 9.4 Non-streaming retry

Full retry loop with exponential backoff for both stream and non-stream branches. Retries on HTTP 429/502/503/504 and TimeoutError. Other HTTP errors (4xx) are returned immediately.

---

## 10. Dependency graph

```
backend-adapter.py
  ├── config.py          (no internal deps on the top level — stdlib only;
  │                       writes probe JSON via probe_json — безусловный канал;
  │                       probe_endpoints/_http_json: HTTP POSTs на бэкенды)
  ├── server.py          → config, redact, daemon, tracer, logger, session_log,
  │                       convert, streaming, model_usage, routing, session_registry
  ├── model_usage.py     → config, yaml, probe_json (used-models table, см. §6.6;
  │                       пер-модельные пробы эндпоинтов пишут JSON-дампы в LOGPATH)
  ├── session_registry.py → config (лист DAG: in-memory таблица сессий агентов
  │                       — строка = кортеж session+agent+model+backend+route,
  │                       секция «Sessions» — v0.9.2)
  ├── probe_json.py      (no internal deps on the top level — stdlib only;
  │                       config/redact читаются локально внутри записи)
  ├── routing.py         → config (лист DAG: входные форматы, TARGET-реестр,
  │                       decide по кэшу endpoint_support — см. §4.2)
  ├── convert.py         → tracer, config
  ├── streaming.py       → tracer, config, logger
  ├── tracer.py          → session_log, config, redact
  ├── logger.py          → config, redact, session_log
  ├── redact.py          (no internal deps — stdlib only)
  ├── session_log.py     (no internal deps — PyYAML)
  ├── daemon.py          (no internal deps — stdlib only)
  ├── webserver.py       → session_viewer, webui_status, webui_config_api, webui_ops
  │                       (WEBUI core: serve() импортирует встроенные
  │                       эндпойнты; CLI python -m backend_adapter.webserver)
  ├── session_viewer.py  → webserver (эндпойнт "/session"), artifact_tree
  ├── webui_status.py    → webserver (эндпоинты "/", "/api/refresh-state",
  │                       "/api/model-usage/snapshot", "/api/model-usage/reset",
  │                       "/api/model-usage/delete", "/api/model-usage/reprobe",
  │                       "/api/model-usage/reprobe-state", "/api/sessions/snapshot"),
  │                       config, model_usage, session_registry
  ├── webui_config_api.py → webserver (эндпойнт "/config"), config (RUNTIME_CONFIG_POOL)
  ├── webui_ops.py       → webserver (эндпоинты "/healthz" "/health" "/live" "/ready"),
  │                       config (readiness: _BACKENDS/_AVAILABLE_MODELS)
  ├── prometheus_exporter.py → config, model_usage (отдельный слушатель:
  │                       НЕ эндпоинт webserver; поднимается backend-adapter.py)
  └── artifact_tree*.py  (8 modules, layered):
      artifact_tree.py (shim) → common, registry, parse, turnbuilder, plantuml, graphviz, html
      ├── artifact_tree_html.py      → common  (цвета ANCHOR/SINK/ORPHAN определены здесь)
      ├── artifact_tree_graphviz.py  → common  (render_png_via_plantuml определён здесь)
      ├── artifact_tree_plantuml.py  → common
      ├── artifact_tree_turnbuilder.py → parse, registry, common
      ├── artifact_tree_parse.py     → common, registry
      ├── artifact_tree_registry.py  → common
      └── artifact_tree_common.py    (no internal deps — stdlib only)
  └── __init__.py        → (lazy proxy, резолвит globals в _get_config_globals())
```

Entry point (WEBUI): `backend-adapter.py` импортирует `webserver.serve()`
для daemon-потока WEBUI (см. §4.1). Вход CLI: `python -m backend_adapter.webserver`.

**Key invariant**: `redact.py`, `session_log.py`, `daemon.py`, `config.py` (env var reads), `artifact_tree_common.py`, and `webserver.py` (endpoint imports only inside `serve()`) have **zero internal package dependencies at import time**, forming the dependency base. All other modules depend on at least one of these.

---

## 11. Configuration summary

All configuration via `ADAPTER_*` environment variables. See `docs/environment.md` for full reference with defaults and descriptions.

---

## 12. Version

Current: **v0.9.0** (see `backend-adapter.py`).
Changelog: `changelog.md` (история версии — секция с её номером).
