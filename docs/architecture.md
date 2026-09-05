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
├── streaming.py            ← SSE streaming: OpenAI SSE → Anthropic SSE
├── tracer.py               ← JSONL trace logging + tool-use causality tracking
├── logger.py               ← human-readable debug logs (_d, _dr)
├── redact.py               ← secret masking (Bearer tokens, *_KEY, *_PAT, etc.)
├── session_log.py          ← per-session log file management with FIFO eviction
├── daemon.py               ← process detachment (double fork + stdio redirect)
├── webserver.py            ← WEBUI core: shared web server, endpoint registry/router,
│                             WebContext, serve(), CLI (python -m backend_adapter.webserver)
├── webui_status.py         ← WEBUI endpoints "/" + "/api/refresh-state": status page
│                             (version, LLM endpoints, models) + background-check state
├── webui_config_api.py     ← WEBUI endpoint "/config": runtime-config form (RUNTIME_CONFIG_POOL)
├── session_viewer.py       ← WEBUI endpoint "/session": *.parts session tabs + file serving
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
Claude Code (Anthropic API client)
       │
       │  POST /v1/messages  (Anthropic format)
       │  GET  /v1/models
       ▼
┌─────────────────────────────────────────────────┐
│              backend-adapter.py                  │
│  (entry: startup backend init → server.serve_forever()) │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│              server.py: Adapter                  │
│                                                  │
│  do_GET  → /v1/models → return _AVAILABLE_MODELS │
│  do_POST → /v1/messages                          │
│    ├─ parse & validate                           │
│    ├─ model mapping (ADAPTER_MODELS_MAPPING)     │
│    ├─ backend resolution (_resolve_backend)      │
│    ├─ tool_result tracing (causality lookup)     │
│    ├─ convert Anthropic → OpenAI body            │
│    │                                            │
│    ├─ [stream mode] → stream_openai_to_anthropic │
│    │       │  urllib → backend SSE reader        │
│    │       │  _sse_write → wfile (client)        │
│    │       │  stream converter (chunk-by-chunk)  │
│    │                                            │
│    └─ [non-stream] → convert_openai_to_anthropic │
│               │  urllib → backend full response  │
│               │  JSON convert                    │
│               │  _send_json → client             │
│                                                  │
│  Cross-cutting:                                  │
│  • _d() / _dr()     → logger + redact            │
│  • _trace()         → tracer + redact            │
│  • session_log      → per-session file handles   │
│  • retry loop       │  exponential backoff       │
│  • ADAPTER_TIMEOUT  │  per-attempt timeout       │
└──────────────────────┬──────────────────────────┘
                       │
                       │  POST /v1/chat/completions  (OpenAI format)
                       │  Bearer <key>
                       ▼
              OpenAI-compatible backend
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
5. Log directory `ADAPTER_DEBUG_LOGPATH` (empty — **file logging off**: console
   debug blocks still visible at `ADAPTER_DEBUG_ENABLE=1`, no directory is created):
   when set, created when `ADAPTER_DEBUG_ENABLE=1` (master switch — at `0` nothing is
   written and the folder is NOT created); points at an existing **file** →
   `[FATAL]` + hint + `sys.exit(1)` (the path is always a directory).
6. If `ADAPTER_WEBUI_ENABLE=1` (default): start the WEBUI in a daemon thread via
   `webserver.serve(root, __version__)` on `ADAPTER_WEBUI_HOST:<ADAPTER_WEBUI_PORT>`
   (default `127.0.0.1` — localhost only; `0.0.0.0` — access from the network,
   careful with session contents) where `root` = `ADAPTER_DEBUG_LOGPATH` when set,
   otherwise an independent `./tmp/webui` (created on demand — status page `/`
   works out of the box; `/session` is empty until logs exist; endpoints: `/` —
   status, `/session` — session viewer, `/config` — runtime-config form,
   `/api/refresh-state` — JSON state of the background check, see §6.5).
   Loading the status page `/` (GET) renders the current state
   (`config.refresh_state()`: models from the startup probe, or from the last
   check) and — if no check has run yet (`done_at` is empty) — starts the
   **first** check automatically (`webui_status._autostart_first_check`).
   Checks are background (`config.start_refresh` runs `config.refresh_models`,
   5 s timeout per endpoint, in a daemon thread): started at adapter startup,
   on the first GET `/`, and by the «⟳ Проверить сейчас» button (POST `/`),
   which answers **303 See Other** → GET `/` (PRG pattern — page reloads
   never repeat the POST, no «resubmit» dialog). While a check runs, the page
   shows a «Проверка выполняется…» banner and polls `/api/refresh-state`;
   when the check finishes, JS reloads the page (`location.reload()`), which
   renders the fresh `_AVAILABLE_MODELS`/`_MODEL_TO_BACKEND` caches — models
   added by the backend after startup are picked up without restarting the
   adapter.
   The `/config` endpoint toggles the runtime debug-write pool
   (`config.get_runtime_config`/`set_runtime_config`, see §8.6) without a restart.

### 4.2 POST /v1/messages (do_POST, server.py:148–581)

```
1. Extract session_id, req_id, update session_log context
2. Parse & validate request JSON (require "model" field)
3. Strict model validation (ADAPTER_STRICT_MODELS)
4. Model mapping (ADAPTER_MODELS_MAPPING string → dict)
5. Backend resolution (_resolve_backend)
   - Explicit prefix (<backend>.model) → strip, route
   - Lookup in _MODEL_TO_BACKEND
   - Fallback → _DEFAULT_BACKEND
6. Trace tool_results from incoming messages (causality: tool_use_id → parent req_id)
7. Convert Anthropic → OpenAI (messages, tools, tool_choice, system)
8. Determine stream mode (client stream flag × ADAPTER_STREAMING_ENABLE)
9. Retry loop (ADAPTER_RETRY times, exponential backoff):
   ├─ Stream branch: urllib urlopen → _start_sse() → stream_openai_to_anthropic()
   │   └─ Chunk-by-chunk SSE conversion, write Anthropic SSE events to wfile
   └─ Non-stream branch: urllib urlopen → read full → convert_openai_to_anthropic()
       └─ Single JSON response → _send_json()
10. Error handling:
    ├─ HTTPError (retry on 429/502/503/504 only)
    ├─ TimeoutError (retry)
    ├─ BrokenPipe/ConnectionReset (client gone — silent log)
    └─ Unexpected exceptions (streamed: SSE "error" event; non-streamed: JSON error)
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
Модель на эндпоинт — из необязательного ключа `probe` YAML-записи (у разных
эндпоинтов бэкенда свои модели), иначе первая модель бэкенда из `/v1/models`;
заданная в `probe` модель, отсутствующая среди моделей бэкенда, пропускает
только свой эндпоинт (`[WARN]` + текст в `errors`).

Проба встроена в `config.refresh_models` (конец функции, после обновления кэша
моделей): вызывается при каждой фоновой проверке бэкендов — при старте
адаптера, на первом GET `/` и по кнопке «⟳ Проверить сейчас» (POST `/` →
`config.start_refresh` → фоновый воркер, см. §6.5). Результат добавляется в
возвращаемый dict ключом `"probe"` (старые читатели `ok/count/errors` не
ломаются) и кэшируется в `_ENDPOINT_STATE` (~60 с, `ENDPOINT_PROBE_TTL`);
повторная проверка в пределах TTL сеть не трогает. Мастер-флаг
`ADAPTER_ENDPOINT_PROBE=0` отключает автопробу.
Фактическая проба пишется в консоль блоком `[ENDPOINT_PROBE]` (print, гейт
`ADAPTER_DEBUG_ENABLE`) — одна строка на бэкенд с сырыми HTTP-кодами; кэш-хиты
не логируются. Проба чисто наблюдательная: на маршрутизацию запросов не влияет.

### 6.5 Фоновая проверка бэкендов (start_refresh / refresh_state, config.py)

Раньше каждый GET/POST статус-страницы `/` синхронно гонял
`config.refresh_models` (опрос `/v1/models` всех бэкендов + дымовая проба), и
при недоступном/медленном бэкенде HTTP-ответ висел (N бэкендов × 5 с на
эндпоинт). Теперь проверка — **фоновая**, запускается при старте адаптера, на первом
GET `/` и по кнопке:

- **Состояние** — модульный снимок-словарь `_REFRESH_JOB` в `config.py`:
  `running`, `started_at`/`done_at` (time.time), `ok`/`count`/`errors` (итог
  последней `refresh_models`), `checked_at` («HH:MM:SS» завершения). Снимок
  **иммутабелен** — заменяется целиком (атомарная замена ссылки), читатели
  (`webui_status`) берут `refresh_state()` без лока. До первой проверки —
  дефолт-словарь (все None/False).
- **Запуск** — `start_refresh(timeout)`: при старте адаптера
  (`backend-adapter.py` после поднятия WEBUI), на первом GET `/`
  (автостарт, `webui_status._autostart_first_check`) и по кнопке
  «⟳ Проверить сейчас» (POST `/`). Под `_REFRESH_LOCK` публикует
  `running=True` и стартует daemon-поток `_refresh_worker`; пока проверка
  идёт, повторный вызов возвращает `False` (второй поток не создаётся).
  `_refresh_worker` зовёт `refresh_models(timeout)` и в `finally` публикует
  финальный снимок (`running=False`, результат или текст исключения в
  `errors["__worker__"]`) — проверка не может «зависнуть навсегда».
  HTTP-ответ не блокируется.
- **Страница** (`webui_status.py`): GET `/` читает `config.refresh_state()` и
  рендерит последний результат; если проверок ещё не было (done_at пуст) и
  есть что проверять — первый заход сам запускает ПЕРВУЮ проверку
  (`_autostart_first_check`), повторные — только по кнопке. POST `/`
  (кнопка «⟳ Проверить сейчас») вызывает `start_refresh(timeout=
  PROBE_TIMEOUT)` и отвечает **303 See Other** на GET `/` (PRG: браузер
  переходит на страницу GET-навигацией, авто-релоад не повторяет POST).
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

### 6.2 Разрешение коллизий имён моделей

Когда одна и та же модель встречается на нескольких бэкендах, генерируется префиксный ID:

```
<backend_name>.<model_id>  →  e.g.  "home.qwen3.6-35b-a3b"
```

Префикс получают только конфликтующие ID; уникальные ID проходят без изменений.

### 6.3 Логика маршрутизации (`_resolve_backend`, config.py:435–475)

1. **Явный префикс** — снять префикс `<backend_name>.`, направить на соответствующий бэкенд
2. **Lookup по списку моделей** — поиск в `_MODEL_TO_BACKEND`
3. **Fallback** — первый бэкенд в конфиге (`_DEFAULT_BACKEND`); если бэкенд не сконфигурирован/не найден — `RuntimeError`

---

## 7. Tool-use causality tracking

Claude Code's agent loop can send **parallel requests** within a single session (e.g., main agent turn + structured_output sidebar). Reconstructing "which request produced tool_use X, which request returned its tool_result" from timestamps alone is unreliable.

Solution: `tool_use_id` is the natural unique key.

1. **Register** (`_register_tool_use`): when converting OpenAI → Anthropic response, record `(session_id, tool_use_id, req_id)` in an `OrderedDict` per session
2. **Lookup** (`_lookup_tool_use_producer`): when processing incoming `tool_result`, find the `req_id` that produced the corresponding `tool_use`
3. **Eviction**: FIFO eviction at `_TOOL_USE_INDEX_MAX_PER_SESSION` (2000 entries) to bound memory for long sessions

Trace event `tool_result` includes `parent_req_id` — `null` if the producer was evicted or never recorded.

---

## 8. Observability stack

### 8.1 Debug logging (`logger.py`)

`_d(msg)` — timestamped message, optionally prefixed with `[req_id]` (`_dr`)

- Goes to stdout if `ADAPTER_DEBUG_ENABLE=1` (master switch — console blocks are
  independent of disk logging)
- Written to per-session `session-<ts>-<sid>.log` files in the `ADAPTER_DEBUG_LOGPATH`
  directory (only when set — empty means file logging is off, no directory is
  created) when `ADAPTER_DEBUG_ENABLE=1`
- All messages pass through `redact()` to mask secrets (unless `ADAPTER_SENSITIVE_LOGGING_ENABLE=1`)

The write-volume flags below are **runtime-switchable**: the WEBUI endpoint
`/config` re-reads them live (see §8.6) instead of restarting the adapter.

Debug flags:

| Flag | Purpose |
|---|---|
| `ADAPTER_DEBUG_BODY_FULL` | Full Anthropic request body (no trim) |
| `ADAPTER_DEBUG_OPENAI_BODY_FULL` | Full OpenAI request body |
| `ADAPTER_DEBUG_RESPONSE_FULL` | Full responses (both stream and non-stream) |
| `ADAPTER_DEBUG_TOOLS` | Full tool result content for all results |
| `ADAPTER_DEBUG_TOOLS_ERROR` | Full tool error details (default: off — zero-config does not process collected logs) |
| `ADAPTER_DEBUG_TOOLS_RESPONSE_FULL` | No trim on tool result content |
| `ADAPTER_DEBUG_TRIM` | Max chars for trimmed logs (default: 3000) |

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

### 8.5 Per-request OpenAI body JSON dump (`ADAPTER_DEBUG_OPENAI_BODY_JSON`)

When enabled, writes complete OpenAI-format request bodies as numbered JSON files alongside the session log files.

- Only activates when `ADAPTER_DEBUG_ENABLE=1` (master switch) and the log directory
  `ADAPTER_DEBUG_LOGPATH` is set (created on demand)
- Creates `session-<datetime>-<sessid8>.parts/` directory next to the session log files
- Writes `openai-NNNN.json` for each POST `/v1/messages` request with full body
- Thread-safe: uses `threading.Lock` on the per-session counter
- Uses `json.dump(indent=2)` for readable formatting

### 8.6 Runtime config pool + WEBUI endpoint `/config` (`webui_config_api.py`)

`config.py` keeps a narrow `RUNTIME_CONFIG_POOL` — variables controlling only the
volume of disk writes, safe to flip without restarting the adapter:

| Type | Variables |
|---|---|
| bool | `ADAPTER_DEBUG`, `ADAPTER_DEBUG_TAGS_OUT`, `ADAPTER_DEBUG_TOOLS`, `ADAPTER_DEBUG_TOOLS_ERROR` |
| int | `ADAPTER_TRACE_REASONING_MAX_CHARS`, `ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS`, `ADAPTER_DEBUG_TRIM` |

- `get_runtime_config()` — snapshot dict `{name: value}`; `set_runtime_config(**kw)`
  type-validates against `_RUNTIME_CONFIG_TYPES` and silently ignores out-of-pool
  keys / wrong types (bool is checked before int — `bool` subclasses `int`), then
  re-assigns `config` module globals. Returns the post-state dict.
- **Readers must read live** — `config.ADAPTER_X` module attribute at call time,
  not `from .config import X` import-time snapshots. All pool consumers were
  refactored to live reads: `logger.py` (`_d` gating), `server.py`,
  `convert.py`, `streaming.py`, `tracer.py`.
- Deliberately **excluded** from the pool: network, backends, models, ports,
  `ADAPTER_DEBUG_LOGPATH` (directory identity must not change mid-flight — the
  session logger would write to a moving target).
- Endpoint `webui_config_api.py` (`@webserver.register`, prefix `/config`): GET —
  HTML form (4 bool checkboxes + 3 int inputs) with current pool values; POST —
  `application/x-www-form-urlencoded` or JSON body → `set_runtime_config()`,
  response re-renders with a flash «Применено»/«Игнорировано» split. Links to
  the form: session tabs panel («config») and the status page `/` («runtime config →»).
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
  ├── config.py          (no internal deps — stdlib only + os.environ;
  │                       probe_endpoints/_http_json: HTTP POSTs на бэкенды)
  ├── server.py          → config, redact, daemon, tracer, logger, session_log, convert, streaming
  ├── convert.py         → tracer, config
  ├── streaming.py       → tracer, config, logger
  ├── tracer.py          → session_log, config, redact
  ├── logger.py          → config, redact, session_log
  ├── redact.py          (no internal deps — stdlib only)
  ├── session_log.py     (no internal deps — PyYAML)
  ├── daemon.py          (no internal deps — stdlib only)
  ├── webserver.py       → session_viewer, webui_status, webui_config_api (WEBUI core: serve()
  │                       импортирует встроенные эндпойнты; CLI python -m backend_adapter.webserver)
  ├── session_viewer.py  → webserver (эндпойнт "/session"), artifact_tree
  ├── webui_status.py    → webserver (эндпойнты "/", "/api/refresh-state"), config
  ├── webui_config_api.py → webserver (эндпойнт "/config"), config (RUNTIME_CONFIG_POOL)
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

Current: **v0.8.2** (see `backend-adapter.py`).
Changelog: `changelog.md` (история версии — секция с её номером).
