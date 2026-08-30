#!/usr/bin/env python3
"""Claude Code <-> OpenAI-backend adapter v0.6.4
— changelog: ../changelog.md"""
__version__ = "0.6.4"
__comment__ = "streaming SSE passthrough + keep-alive fix + timeout+retry+trace+causality + per-session logs + model probe/validation + unbuffered I/O + multi-backend config + clean _fetch_models + stream usage/input_tokens fix + domain package refactoring + HTTP log req_id + SSE response logging + unified response full logging flag + tool result debug logging"

import os
import sys
import threading
from collections import Counter

# ==================== Package imports ====================
from backend_adapter.config import (
    BACKEND_BASE, BACKEND_KEY, PROXY_PORT,
    ADAPTER_DEBUG, ADAPTER_DEBUG_LOGFILE, ADAPTER_TRACE_LOGFILE,
    ADAPTER_DETACH, ADAPTER_TIMEOUT, ADAPTER_RETRY,
    ADAPTER_SKILL_PATTERNS, ADAPTER_DEBUG_TRIM,
    ADAPTER_DEBUG_BODY_FULL, ADAPTER_DEBUG_OPENAI_BODY_FULL,
    ADAPTER_DEBUG_RESPONSE_FULL,
    ADAPTER_DEBUG_TOOLS, ADAPTER_DEBUG_TOOLS_ERROR, ADAPTER_DEBUG_TOOLS_RESPONSE_FULL,
    ADAPTER_TRACE_REASONING_MAX_CHARS, ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS,
    ADAPTER_STRICT_MODELS,
    ADAPTER_STREAMING_ENABLE, ADAPTER_STREAM_INCLUDE_USAGE,
    ADAPTER_MODELS_MAPPING,
    ADAPTER_BACKEND_CONFIG,
    _BACKEND_LEGACY,
    _BACKENDS, _BACKEND_BY_NAME, _MODEL_TO_BACKEND, _DEFAULT_BACKEND,
    _parse_models_mapping, _MAP,
    _parse_backend_yaml,
    _fetch_models, _probe_models, _init_multi_backends,
    _AVAILABLE_MODELS, _cap,
    SSL_CTX,
    _resolve_backend,
)
from backend_adapter.redact import redact, redact_headers
from backend_adapter.daemon import _detach, _write_pidfile
from backend_adapter.logger import _d, _dr
from backend_adapter.tracer import (
    _trace_lock, _session_seq, _next_seq,
    _TOOL_USE_INDEX_MAX_PER_SESSION, _tool_use_producers,
    _register_tool_use, _lookup_tool_use_producer, _trace,
)
from backend_adapter.skill import (
    DEFAULT_SKILL_PATTERNS, SKILL_PATTERNS,
    _load_skill_patterns, detect_skill,
)
from backend_adapter.convert import (
    extract_text,
    convert_tools_anthropic_to_openai,
    convert_tool_choice_anthropic_to_openai,
    extract_tool_results,
    convert_messages_anthropic_to_openai,
    parse_tool_calls_from_text,
    convert_openai_to_anthropic,
)
from backend_adapter.streaming import _sse_write, stream_openai_to_anthropic
from backend_adapter.server import Adapter, QuietThreadingHTTPServer
# session_log — globals only (functions used internally by module)
from backend_adapter import session_log




if __name__ == "__main__":
    if ADAPTER_DETACH:
        print(f"[DETACH] Starting as background service...")
        print(f"Timeout:  {ADAPTER_TIMEOUT}s")
        print(f"Retries:  {ADAPTER_RETRY}")
        _detach()
        _write_pidfile()

    print(f"\n{'='*70}")
    print(f"Claude Code Adapter v{__version__} ({__comment__})")
    print(f"Listening:  http://localhost:{PROXY_PORT}")
    print(f"Trace log:  {ADAPTER_TRACE_LOGFILE or '(disabled)'}")
    print(f"Models:     {'strict' if ADAPTER_STRICT_MODELS else 'permissive'} validation")
    print(f"Streaming:  {'enabled (SSE passthrough)' if ADAPTER_STREAMING_ENABLE else 'disabled (legacy stream=False, старое поведение)'}")

    if _BACKEND_LEGACY:
        # === Legacy single-backend ===
        print(f"Backend:    {BACKEND_BASE}/v1/chat/completions")
        print(f"Retries:    {ADAPTER_RETRY}")
        print(f"{'='*70}\n")

        try:
            backend_models = _probe_models()
            if backend_models:
                for m in backend_models:
                    mid = m.get("id", "unknown")
                    _AVAILABLE_MODELS[mid] = m
                print(f"[INIT] Loaded {len(_AVAILABLE_MODELS)} models from backend:")
                for m in backend_models:
                    print(f"  - {m.get('id', '?')}")
            else:
                print("[WARN] Backend returned empty model list — models validation disabled.")
        except Exception as e:
            print(f"[FATAL] Failed to probe backend models at startup: {e}")
            print("Adapter cannot start without knowing available models. Exiting.")
            sys.exit(1)
    else:
        # === Multi-backend ===
        _d(f"[INIT] Multi-backend mode: config={ADAPTER_BACKEND_CONFIG}")
        try:
            _init_multi_backends(ADAPTER_BACKEND_CONFIG)
        except Exception as e:
            print(f"[FATAL] Failed to initialize multi-backend: {e}")
            print("Adapter cannot start. Exiting.")
            sys.exit(1)

        print(f"Backends:   {len(_BACKENDS)} configured:")
        for b in _BACKENDS:
            print(f"  - {b['name']}: {b['base']}")
        print(f"{'='*70}\n")

    # ThreadingHTTPServer вместо socketserver.TCPServer: Claude Code может
    # открывать несколько параллельных запросов (конкурентные tool calls),
    # а однопоточный сервер обрабатывает их строго последовательно — пока
    # первый запрос ждёт ADAPTER_TIMEOUT секунд от бэкенда, остальные
    # соединения простаивают в очереди accept() и клиент рвёт их по своему
    # таймауту. Это и есть основной источник BrokenPipeError в логе.
    Adapter.daemon_threads = True  # type: ignore[attr-defined]
    with QuietThreadingHTTPServer(("", PROXY_PORT), Adapter) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[EXIT] Bye")
