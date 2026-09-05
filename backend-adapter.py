#!/usr/bin/env python3
"""[CC] <-> [OI]-backend adapter v0.8.1
— changelog: ../changelog.md"""

__version__ = "0.8.1"
__comment__ = "streaming SSE passthrough + keep-alive fix + timeout+retry+trace+causality + per-session logs + model probe/validation + unbuffered I/O + multi-backend config + clean _fetch_models + stream usage/input_tokens fix + domain package refactoring + HTTP log req_id + SSE response logging + unified response full logging flag + tool result debug logging + per-request OpenAI body JSON dump + JSON parts dir/session-file naming fix + tool_name in TOOL_RESULT_ERROR log + tool_name in TOOL_RESULT (successful) + merged ADAPTER_DEBUG_TAGS_OUT flag + WEBUI session viewer (artifact tree visualization) + shared web-server core + /session endpoint + / status page + console entry point + CI/PR scaffold + zero-config defaults: console-only logs (no disk dir), TOOLS_ERROR off, WEBUI status page on by default + distribution: standalone binaries (PyInstaller), build script, CI release workflow, one-line installer install.sh + runtime-config endpoint /config (live reads config.X) + incremental artifact-tree builds with checkpoints (.build_state.json) + pagination pages artefacts/pages/<N>/ + /session hash8 URL aliases + png/puml shortcuts + skill detection removed (skill.py, ADAPTER_SKILL_PATTERNS, skill_signal)"

import os
import sys
import threading
from collections import Counter

# ==================== Package imports ====================
from backend_adapter.config import (
    PROXY_PORT,
    ADAPTER_ENDPOINT_HOST,
    ADAPTER_DEBUG,
    ADAPTER_DEBUG_LOGPATH,
    ADAPTER_DETACH,
    ADAPTER_TIMEOUT,
    ADAPTER_RETRY,
    ADAPTER_DEBUG_TRIM,
    ADAPTER_DEBUG_TOOLS,
    ADAPTER_DEBUG_TOOLS_ERROR,
    ADAPTER_TRACE_REASONING_MAX_CHARS,
    ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS,
    ADAPTER_STRICT_MODELS,
    ADAPTER_WEBUI_ENABLE,
    ADAPTER_WEBUI_HOST,
    ADAPTER_WEBUI_PORT,
    ADAPTER_STREAMING_ENABLE,
    ADAPTER_STREAM_INCLUDE_USAGE,
    ADAPTER_MODELS_MAPPING,
    ADAPTER_BACKEND_CONFIG,
    _BACKENDS,
    _BACKEND_BY_NAME,
    _MODEL_TO_BACKEND,
    _DEFAULT_BACKEND,
    _parse_models_mapping,
    _MAP,
    _parse_backend_yaml,
    _init_multi_backends,
    _AVAILABLE_MODELS,
    _cap,
    SSL_CTX,
    _resolve_backend,
)
from backend_adapter.redact import redact, redact_headers
from backend_adapter.daemon import _detach, _write_pidfile
from backend_adapter.logger import _d, _dr
from backend_adapter.tracer import (
    _trace_lock,
    _session_seq,
    _next_seq,
    _TOOL_USE_INDEX_MAX_PER_SESSION,
    _tool_use_producers,
    _register_tool_use,
    _lookup_tool_use_producer,
    _trace,
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
        print("[DETACH] Starting as background service...")
        print(f"Timeout:  {ADAPTER_TIMEOUT}s")
        print(f"Retries:  {ADAPTER_RETRY}")
        _detach()
        _write_pidfile()

    print(f"\n{'=' * 70}")
    print(f"Claude Code Adapter v{__version__} ({__comment__})")
    print(f"Listening:  http://{ADAPTER_ENDPOINT_HOST}:{PROXY_PORT}")
    if not ADAPTER_DEBUG:
        log_status = "disabled (ADAPTER_DEBUG_ENABLE=0)"
    elif ADAPTER_DEBUG_LOGPATH:
        log_status = f"{ADAPTER_DEBUG_LOGPATH} (диск)"
    else:
        log_status = "console only (ADAPTER_DEBUG_ENABLE=1; диск: задайте ADAPTER_DEBUG_LOGPATH)"
    print(f"Logs:       {log_status}")
    print(f"Models:     {'strict' if ADAPTER_STRICT_MODELS else 'permissive'} validation")
    print(
        f"Streaming:  {'enabled (SSE passthrough)' if ADAPTER_STREAMING_ENABLE else 'disabled (legacy stream=False, старое поведение)'}"
    )

    if not ADAPTER_BACKEND_CONFIG:
        print(
            "[FATAL] ADAPTER_BACKEND_CONFIG is not set. Задайте путь к YAML-файлу "
            "конфигурации бэкенда (пример — sample.adapter.yaml в корне репозитория)."
        )
        sys.exit(1)

    # === Backend config ===
    _d(f"[INIT] Backend config: {ADAPTER_BACKEND_CONFIG}")
    try:
        _init_multi_backends(ADAPTER_BACKEND_CONFIG)
    except Exception as e:
        print(f"[FATAL] Failed to initialize backends: {e}")
        print("Adapter cannot start. Exiting.")
        sys.exit(1)

    print(f"Backends:   {len(_BACKENDS)} configured:")
    for b in _BACKENDS:
        print(f"  - {b['name']}: {b['base']}")
    print(f"{'=' * 70}\n")

    # === Log directory (ADAPTER_DEBUG_LOGPATH) ===
    # Файловая запись включается ТОЛЬКО явно заданным ADAPTER_DEBUG_LOGPATH
    # (zero-config: путь пуст → ничего не пишется на диск и папка НЕ создаётся,
    # консольные debug-блоки видны всегда при ADAPTER_DEBUG_ENABLE=1).
    # Заданный путь всегда директория: проверяем, что это не файл, и создаём
    # папку при необходимости.
    if ADAPTER_DEBUG and ADAPTER_DEBUG_LOGPATH:
        log_path = ADAPTER_DEBUG_LOGPATH
        if os.path.isfile(log_path):
            print(
                f"[FATAL] ADAPTER_DEBUG_LOGPATH указывает на файл, а нужна директория: "
                f"{log_path!r}. Логи сессий и трейсов, *.parts дампы и корень "
                "веб-интерфейса живут в одной папке — задайте путь к директории."
            )
            sys.exit(1)
        os.makedirs(log_path, exist_ok=True)

    # ThreadingHTTPServer вместо socketserver.TCPServer: [CC] может
    # открывать несколько параллельных запросов (конкурентные tool calls),
    # а однопоточный сервер обрабатывает их строго последовательно — пока
    # первый запрос ждёт ADAPTER_TIMEOUT секунд от бэкенда, остальные
    # соединения простаивают в очереди accept() и клиент рвёт их по своему
    # таймауту. Это и есть основной источник BrokenPipeError в логе.
    # Веб-интерфейс WEBUI (webserver.py — общее ядро; эндпойнты:
    # session_viewer.py "/session" + webui_status.py "/") — отдельный поток
    # внутри процесса адаптера: daemon-поток, живёт вместе с адаптером.
    # Корень НЕ обязан быть директорией логов: при заданном ADAPTER_DEBUG_LOGPATH
    # — это она (там лежат *.parts папки сессий); при пустом — независимая
    # папка ./tmp/webui (статус-страница работает всегда, /session пуст).
    if ADAPTER_WEBUI_ENABLE:
        webui_root = ADAPTER_DEBUG_LOGPATH or "./tmp/webui"
        os.makedirs(webui_root, exist_ok=True)
        from backend_adapter.webserver import serve as webui_serve

        webui = webui_serve(
            webui_root,
            __version__,
            ADAPTER_WEBUI_HOST,
            ADAPTER_WEBUI_PORT,
            verbose=False,
        )
        if webui:
            threading.Thread(target=webui.serve_forever, daemon=True).start()
            print(f"[WEBUI] http://{ADAPTER_WEBUI_HOST}:{ADAPTER_WEBUI_PORT}/ (root: {webui_root})")
    Adapter.daemon_threads = True  # type: ignore[attr-defined]
    with QuietThreadingHTTPServer((ADAPTER_ENDPOINT_HOST, PROXY_PORT), Adapter) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[EXIT] Bye")
