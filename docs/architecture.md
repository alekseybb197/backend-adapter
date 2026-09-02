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
backend-adapter.py          ← entry point (startup, probe models, start server)
backend_adapter/
├── __init__.py             ← lazy proxy for module-level globals
├── config.py               ← env vars, model mapping, multi-backend routing, YAML parser, probe
├── server.py               ← HTTP handler (Adapter), QuietThreadingHTTPServer
├── convert.py              ← Anthropic ↔ OpenAI conversion functions
├── streaming.py            ← SSE streaming: OpenAI SSE → Anthropic SSE
├── tracer.py               ← JSONL trace logging + tool-use causality tracking
├── logger.py               ← human-readable debug logs (_d, _dr)
├── redact.py               ← secret masking (Bearer tokens, *_KEY, *_PAT, etc.)
├── session_log.py          ← per-session log file management with FIFO eviction
├── skill.py                ← skill detection from tool call argument patterns
├── daemon.py               ← process detachment (double fork + stdio redirect)
├── session_viewer.py       ← WEBUI: local web server over *.parts sessions (tabs + tree.html)
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
│  (entry: startup probe → server.serve_forever()) │
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

### 4.1 Startup (`backend-adapter.py:64–127`)

1. If `ADAPTER_DETACH=1` — double-fork daemonize + write PID file
2. Parse env config → `backend_adapter/config.py` (all env vars with `ADAPTER_` prefix)
3. Probe backend models:
   - **Legacy mode** (`ADAPTER_BACKEND_CONFIG` empty): single `GET /v1/models` call
   - **Multi-backend mode**: parse YAML config (`_parse_backend_yaml`), probe each backend, resolve model collisions by prefixing with `<backend_name>.`
4. Start `QuietThreadingHTTPServer` on `0.0.0.0:<PROXY_PORT>`
5. If `ADAPTER_WEBUI_ENABLE=1` and `ADAPTER_DEBUG_LOGFILE` is a directory: start `session_viewer` web server in a daemon thread on `127.0.0.1:<ADAPTER_WEBUI_PORT>` (root = `ADAPTER_DEBUG_LOGFILE`)

### 4.2 POST /v1/messages (do_POST, server.py:148–581)

```
1. Extract session_id, req_id, update session_log context
2. Parse & validate request JSON (require "model" field)
3. Strict model validation (ADAPTER_STRICT_MODELS)
4. Model mapping (ADAPTER_MODELS_MAPPING string → dict)
5. Backend resolution (_resolve_backend)
   - Legacy → single backend, model unchanged
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

Accumulates text, reasoning_content, and tool_calls buffers across chunks, then emits aggregate trace/skill/log events at stream end.

**Usage fix**: requests `stream_options.include_usage=true` from backend (if `ADAPTER_STREAM_INCLUDE_USAGE=1`). If backend doesn't return usage, falls back to heuristic `chars // 4` and marks as estimated in trace.

---

## 6. Multi-backend routing

### 6.1 Configuration

Path to YAML file via `ADAPTER_BACKEND_CONFIG`. Minimal YAML parser (`_parse_backend_yaml`) handles:

```yaml
backend:
  - name: home
    base: "http://127.0.0.1:8002"
    key: ADAPTER_HOME_KEY    # env var name → resolved at parse time
  - name: litellm
    base: "https://llm.example.com"
    key: ADAPTER_LITELLM_KEY
```

### 6.2 Collision resolution

When the same model ID appears on multiple backends, a prefixed ID is generated:

```
<backend_name>.<model_id>  →  e.g.  "home.qwen3.6-35b-a3b"
```

Only conflicting IDs get prefixed; unique IDs pass through unchanged.

### 6.3 Routing logic (`_resolve_backend`, config.py:307–348)

1. **Legacy mode** — return single backend, model unchanged
2. **Explicit prefix** — strip `<backend_name>.` prefix, route to matching backend
3. **Model lookup** — search `_MODEL_TO_BACKEND` dict
4. **Fallback** — first backend in config (`_DEFAULT_BACKEND`)

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

- Goes to stdout if `ADAPTER_DEBUG_ENABLE=1`
- Written to file if `ADAPTER_DEBUG_LOGFILE` set
- Per-session files if logfile points to a directory
- All messages pass through `redact()` to mask secrets (unless `ADAPTER_SENSITIVE_LOGGING_ENABLE=1`)

Debug flags:

| Flag | Purpose |
|---|---|
| `ADAPTER_DEBUG_BODY_FULL` | Full Anthropic request body (no trim) |
| `ADAPTER_DEBUG_OPENAI_BODY_FULL` | Full OpenAI request body |
| `ADAPTER_DEBUG_RESPONSE_FULL` | Full responses (both stream and non-stream) |
| `ADAPTER_DEBUG_TOOLS` | Full tool result content for all results |
| `ADAPTER_DEBUG_TOOLS_ERROR` | Full tool error details (default: on) |
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
| `skill_signal` | Skill detected in tool call |
| `tool_call_fallback` | JSON extracted from text (not native tool calling) |
| `usage_report` | Token usage (stream mode) |
| `adapter_invariant_check` | System message position validation |

Output:
- Per-session JSONL files if `ADAPTER_TRACE_LOGFILE` is a directory
- Single JSONL file if it's a full path
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

- File naming: `session-<YYYYMMDD-HHMMSS>-<sessionID_short>.<ext>`
- Timestamp frozen on first use per session (all traffic → same file)
- FIFO eviction at `_LOG_FILES_PER_SESSION` (5000 entries)
- Handles both per-session directory and single-file modes

### 8.5 Per-request OpenAI body JSON dump (`ADAPTER_DEBUG_OPENAI_BODY_JSON`)

When enabled, writes complete OpenAI-format request bodies as numbered JSON files alongside the session log files.

- Only activates when per-session mode is enabled (`ADAPTER_DEBUG=1` + `ADAPTER_DEBUG_LOGFILE` points to a directory)
- Creates `session-<datetime>-<sessid8>.parts/` directory next to the session log files
- Writes `openai-NNNN.json` for each POST `/v1/messages` request with full body
- Thread-safe: uses `threading.Lock` on the per-session counter
- Uses `json.dump(indent=2)` for readable formatting

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

## 10. Skill detection

When the adapter receives a tool call from the backend, it extracts argument fields (`command`, `file_path`, `path`, `pattern`, `notebook_path`) and checks them against regex patterns.

Default patterns:

| Skill | Matches |
|---|---|
| `devtools` | `.claude/skills/devtools`, `.qwen/skills/devtools`, `chrome-devtools` |
| `frontmatter` | `.claude/skills/frontmatter`, `.qwen/skills/frontmatter` |
| `klast` | `.claude/skills/klast`, `.qwen/skills/klast`, `.klast/` |
| `mytasks` | `.claude/skills/mytasks`, `.qwen/skills/mytasks` |
| `prreview` | `.claude/skills/prreview`, `.qwen/skills/prreview` |

Custom patterns loaded from JSON file via `ADAPTER_SKILL_PATTERNS`.

When detected, a `skill_signal` trace event is emitted with the skill name and matching evidence.

---

## 11. Dependency graph

```
backend-adapter.py
  ├── config.py          (no internal deps — stdlib only + os.environ)
  ├── server.py          → config, redact, daemon, tracer, logger, session_log, convert, streaming
  ├── convert.py         → tracer, skill, config
  ├── streaming.py       → tracer, skill, config, logger
  ├── tracer.py          → session_log, config, redact
  ├── logger.py          → config, redact, session_log
  ├── redact.py          (no internal deps — stdlib only)
  ├── session_log.py     (no internal deps — PyYAML)
  ├── skill.py           → config
  ├── daemon.py          (no internal deps — stdlib only)
  ├── session_viewer.py  → artifact_tree (WEBUI; entry point backend-adapter.py:124
  │                       импортирует session_viewer.serve() для daemon-потока)
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

**Key invariant**: `redact.py`, `session_log.py`, `daemon.py`, `config.py` (env var reads), and `artifact_tree_common.py` have **zero internal package dependencies**, forming the dependency base. All other modules depend on at least one of these.

---

## 12. Configuration summary

All configuration via `ADAPTER_*` environment variables. See `docs/environment.md` for full reference with defaults and descriptions.

---

## 13. Version

Current: **v0.6.4** (see `backend-adapter.py:4`)
Changelog: `changelog.md`
