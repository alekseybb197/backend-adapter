#!/usr/bin/env python3
"""[CC] <-> [OI]-backend adapter v0.8.6
— changelog: ../changelog.md"""

__version__ = "0.8.6"
__comment__ = "streaming SSE passthrough + keep-alive fix + timeout+retry+trace+causality + per-session logs + model probe/validation + unbuffered I/O + multi-backend config + clean _fetch_models + stream usage/input_tokens fix + domain package refactoring + HTTP log req_id + SSE response logging + unified response full logging flag + tool result debug logging + per-request OpenAI body JSON dump + JSON parts dir/session-file naming fix + tool_name in TOOL_RESULT log + merged ADAPTER_DEBUG_PARTS flag + WEBUI session viewer (artifact tree visualization) + shared web-server core + /session endpoint + / status page + console entry point + CI/PR scaffold + zero-config defaults: console-only logs (no disk dir), TOOLS_ERROR off, WEBUI status page on by default + distribution: standalone binaries (PyInstaller), build script, CI release workflow, one-line installer install.sh + runtime-config endpoint /config (live reads config.X) + incremental artifact-tree builds with checkpoints (.build_state.json) + pagination pages artefacts/pages/<N>/ + /session hash8 URL aliases + png/puml shortcuts + skill detection removed (skill.py, ADAPTER_SKILL_PATTERNS, skill_signal) + endpoint detection: smoke probe of backend API endpoints (ADAPTER_ENDPOINT_PROBE, YAML probe key) + background refresh of backend list (refresh by button, PRG redirect) + endpoint column HTTP-200-only + auto-start check on adapter start + used-models table on WEBUI status page (ADAPTER_MODEL_USAGE_ENABLE, per-model endpoint probe) + traffic counters (bytes sent/recv to backend) + logging reform: console always trimmed (ADAPTER_DEBUG_TRIM) / file always full, TAGS_FULL/TOOLS/TOOLS_ERROR removed, TAGS_OUT→ADAPTER_DEBUG_PARTS (dumps of all logged parts) + row-action icons 🔄⏪🗑 + graceful shutdown on Ctrl-C/SIGTERM (signal handler, repeat-signal force exit, listener shutdown, interrupt-safe usage flush)"

import contextlib
import os
import signal
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
    ADAPTER_TRACE_REASONING_MAX_CHARS,
    ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS,
    ADAPTER_STRICT_MODELS,
    ADAPTER_WEBUI_HOST,
    ADAPTER_WEBUI_PORT,
    ADAPTER_EXPORTER_ENABLE,
    ADAPTER_EXPORTER_PORT,
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
    print(f"Backend-Adapter v{__version__}")
    print(f"Listening:  http://{ADAPTER_ENDPOINT_HOST}:{PROXY_PORT}")
    if ADAPTER_DEBUG:
        log_status = f"{ADAPTER_DEBUG_LOGPATH} (диск, ADAPTER_DEBUG_ENABLE=1)"
    else:
        log_status = "file logging off (ADAPTER_DEBUG_ENABLE=0); console debug always on"
    print(f"Logs:       {log_status}")
    print(f"Models:     {'strict' if ADAPTER_STRICT_MODELS else 'permissive'} validation")
    print(
        f"Streaming:  {'enabled (SSE passthrough)' if ADAPTER_STREAMING_ENABLE else 'disabled (legacy stream=False, старое поведение)'}"
    )

    if not ADAPTER_BACKEND_CONFIG:
        print(
            "[FATAL] ADAPTER_BACKEND_CONFIG is not set. Задайте путь к YAML-файлу "
            "конфигурации бэкенда (пример — docs/samples/sample.adapter.yaml)."
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
    # Единая директория логов сессий (debug/trace/*.parts дампы) И корень
    # веб-интерфейса (WEBUI + model-usage.yaml). LOGPATH всегда непуст
    # (дефолт ./tmp/logs в config): папка создаётся как корень WEBUI всегда,
    # лог-ФАЙЛЫ в неё пишутся только при ADAPTER_DEBUG_ENABLE=1 (см. config).
    # Путь всегда директория: проверяем, что это не файл — путь-файл сломал
    # бы корень WEBUI даже при выключенной файловой записи.
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
    # session_viewer.py "/session" + webui_status.py "/" + webui_ops.py
    # health-эндпоинты) поднимается ВСЕГДА — отдельный daemon-поток внутри
    # процесса адаптера. Корень — директория ADAPTER_DEBUG_LOGPATH (всегда
    # непуст, дефолт ./tmp/logs): там лежат *.parts папки сессий, корень
    # WEBUI и model-usage.yaml.
    webui_root = ADAPTER_DEBUG_LOGPATH
    os.makedirs(webui_root, exist_ok=True)
    from backend_adapter.webserver import serve as webui_serve

    webui = webui_serve(
        webui_root,
        __version__,
        ADAPTER_WEBUI_HOST,
        ADAPTER_WEBUI_PORT,
        verbose=False,
    )
    exporter = None  # поднимается ниже при ADAPTER_EXPORTER_ENABLE
    if webui:
        threading.Thread(target=webui.serve_forever, daemon=True).start()
        print(f"[WEBUI] http://{ADAPTER_WEBUI_HOST}:{ADAPTER_WEBUI_PORT}/ (root: {webui_root})")
        # Стартовая фоновая проверка бэкендов (модели + дымовая проба
        # API-эндпойнтов): первый GET "/" сразу показывает свежие данные,
        # а не пустую колонку Endpoints. Дублирует стартовый опрос
        # _init_multi_backends — приемлемо: один раз, фоново, с таймаутом
        # PROBE_TIMEOUT (10 с на эндпоинт), не ADAPTER_TIMEOUT (300 с).
        # Локальные импорты: скрипт не импортирует webui_status; config
        # связан только именами (не модулем).
        from backend_adapter import config as _cfg
        from backend_adapter.webui_status import PROBE_TIMEOUT

        _cfg.start_refresh(timeout=PROBE_TIMEOUT)
        # Prometheus-экспортёр — отдельный лёгкий слушатель на
        # ADAPTER_EXPORTER_PORT (текст метрик /metrics, text exposition
        # 0.0.4 без библиотек): настройки/статус приложения, таблица
        # настроенных бэкендов, таблица использованных моделей со
        # счётчиками (см. prometheus_exporter.py). Свой слушатель — не
        # эндпоинт WEBUI: /metrics не должен висеть на порту статуса.
        if ADAPTER_EXPORTER_ENABLE:
            from backend_adapter.prometheus_exporter import serve_exporter

            exporter = serve_exporter(
                __version__, ADAPTER_WEBUI_HOST, ADAPTER_EXPORTER_PORT, verbose=False
            )
            if exporter is not None:
                threading.Thread(target=exporter.serve_forever, daemon=True).start()
                print(f"[EXPORTER] http://{ADAPTER_WEBUI_HOST}:{ADAPTER_EXPORTER_PORT}/metrics")
    Adapter.daemon_threads = True  # type: ignore[attr-defined]

    # === Корректное завершение (Ctrl-C / SIGTERM) ===
    # Дефолтный SIGINT кидает KeyboardInterrupt в главный поток — serve_forever
    # выходит, finally выполняет вежливое завершение. Проблема была в том, что
    # ПОВТОРНЫЙ Ctrl-C во время finally (flush_table пишет YAML) прерывал
    # запись: Python кидает KI в главный поток, где бы тот ни был. Решение
    # (вынесено в backend_adapter/shutdown.py — покрыто тестами, см. ADR):
    # (1) SIGTERM перехватывается на KeyboardInterrupt (как Ctrl-C) — иначе
    # systemd/launchd/kill убивают процесс мгновенно, без finally, и
    # usage-хвост теряется; (2) как только начали завершение, SIGINT/SIGTERM
    # переключаются на немедленный os._exit(130) — повторный сигнал не может
    # прервать flush_table, а просто тихо завершает процесс.
    from backend_adapter.shutdown import graceful_shutdown, install_signal_handlers

    install_signal_handlers(graceful=True)

    def _finish():
        # Финальный этап вежливого завершения: остановить фоновую проверку
        # бэкендов (дождаться текущего цикла, чтобы снимок состояния и кэши
        # моделей/эндпоинтов были консистентны на момент выхода; поток daemon
        # — если не успел за таймаут, процесс завершится сам, воркер
        # оборвётся) и сохранить «грязный» хвост таблицы использованных
        # моделей в model-usage.yaml. Устойчив к прерыванию (см. flush_table).
        # Ошибки не пробрасываются: завершение не должно падать.
        try:
            from backend_adapter import model_usage as _model_usage
            from backend_adapter import config as _cfg

            _cfg.stop_refresh(timeout=2.0)
            _model_usage.flush_table()
        except BaseException:  # noqa: BLE001 — завершение не должно падать
            pass

    with QuietThreadingHTTPServer((ADAPTER_ENDPOINT_HOST, PROXY_PORT), Adapter) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass  # Ctrl-C / SIGTERM: ниже — вежливое завершение
        finally:
            # Переключение сигналов + остановка слушателей + фоновой проверки
            # + финальный flush usage-таблицы (см. shutdown.graceful_shutdown:
            # остановки по отдельности глотают ошибки, процедуру прервать
            # нельзя — повторный сигнал уже = немедленный os._exit(130)).
            graceful_shutdown(
                httpd,
                webui,
                exporter,
                ADAPTER_EXPORTER_ENABLE,
                _finish,
            )
            print("\n[EXIT] Bye")
