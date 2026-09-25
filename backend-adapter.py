#!/usr/bin/env python3
"""[CC] <-> [OI]-backend adapter v0.9.11
— changelog: ../changelog.md"""

__version__ = "0.9.11"
# КОНСТАНТНОЕ определение инструмента (v0.9.9): назначение адаптера не меняется
# от фичи к фиче, поэтому перечисление возможностей здесь не поддерживается.
# Формулировка дублируется в README.md первой строкой вводного blockquote;
# при разработке и релизах НЕ меняется без отдельного указания (CLAUDE.md).
__comment__ = (
    "backend router and endpoint adapter: [AN] Messages <-> [OI]-compatible backends for AI agents"
)

import atexit
import contextlib
import os
import signal
import sys
import threading
from collections import Counter

# ==================== CLI (v0.9.11) ====================
# Разбор argv идёт ПЕРВЫМ — до validate_env() и импорта config ниже. Оба
# зависят от env на этапе импорта (validate_env → [FATAL] на мусоре; config
# парсит int(...) на уровне модуля), а --version/--help обязаны отвечать при
# любом окружении. Порядок импортов здесь намеренный: E402 для этого файла
# уже разрешён в pyproject.toml — ровно ради «validate_env до config».
from backend_adapter.cli_args import parse_args

parse_args(sys.argv[1:], __version__)

# ==================== Package imports ====================
# Строгая проверка env ДО импорта config (v0.9.6): config.py читает env на
# уровне модуля и парсит int(...) — невалидное значение (ADAPTER_PROXY_PORT=abc)
# упало бы голым ValueError раньше любой проверки. validate_env даёт [FATAL]
# с понятным текстом и sys.exit(1) (адаптер не стартует). Импортируется
# первым: сам модуль — лист DAG (только stdlib).
from backend_adapter import env_validate

env_validate.validate_env()

from backend_adapter.config import (
    PROXY_PORT,
    ADAPTER_ENDPOINT_HOST,
    ADAPTER_DEBUG,
    ADAPTER_DATA_ROOT,
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
    _init_multi_backends,
)

# config — модулем (не только именами): коллекции бэкендов/моделей
# мутируются на месте, а баннер ниже читает их как атрибуты модуля.
from backend_adapter import config as _config
from backend_adapter.redact import redact, redact_headers
from backend_adapter.daemon import _detach, _remove_pidfile, _write_pidfile
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

# state_store — перманентное состояние runtime-пула (state.yaml, v0.9.6).
# Импорт ради побочного эффекта apply_on_startup ниже (регистрация колбека
# записи) — как модуль, не отдельными именами.
from backend_adapter import state_store


if __name__ == "__main__":
    # Detach (double fork) — только отделение от консоли. PID-файл пишется
    # НИЖЕ, при любом запуске (не только в detach): он нужен, чтобы
    # манипулировать процессом без консоли (контейнер, служба). Порядок
    # важен: _detach() до записи — PID-файл должен хранить PID конечного
    # (грандчайлд-)процесса, а не родителя, который сразу выходит.
    if ADAPTER_DETACH:
        print("[DETACH] Starting as background service...")
        print(f"Timeout:  {ADAPTER_TIMEOUT}s")
        print(f"Retries:  {ADAPTER_RETRY}")
        _detach()

    print(f"\n{'=' * 70}")
    print(f"Backend-Adapter v{__version__}")
    print(f"Listening:  http://{ADAPTER_ENDPOINT_HOST}:{PROXY_PORT}")
    if ADAPTER_DEBUG:
        log_status = f"{ADAPTER_DATA_ROOT} (диск, ADAPTER_DEBUG_ENABLE=1)"
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

    # === Data root (ADAPTER_DATA_ROOT) и подпапки log/ + var/ ===
    # Корень данных (v0.9.9, переименован из ADAPTER_DEBUG_LOGPATH): корень
    # веб-интерфейса WEBUI, внутри — log/ (сессии: debug/trace/*.parts/.err)
    # и var/ (состояние: PID, model-usage.yaml, state.yaml, *.models.json).
    # Корень всегда непуст (дефолт ./tmp/adapter в config); обе подпапки
    # создаются сразу, лог-ФАЙЛЫ в log/ пишутся только при
    # ADAPTER_DEBUG_ENABLE=1 (см. config).
    # Путь всегда директория: проверяем, что это не файл — путь-файл сломал
    # бы корень WEBUI даже при выключенной файловой записи.
    # Идёт ДО стартовой проверки бэкендов: PID-файл ниже кладётся в var/,
    # а сама проверка — сетевой опрос (может быть долгим), в течение которого
    # процесс уже должен быть адресуемым.
    data_root = ADAPTER_DATA_ROOT
    if os.path.isfile(data_root):
        print(
            f"[FATAL] ADAPTER_DATA_ROOT указывает на файл, а нужна директория: "
            f"{data_root!r}. Логи/трейсы/*.parts сессий живут в log/, состояние "
            "(PID, model-usage.yaml, state.yaml) — в var/ — задайте путь к "
            "директории."
        )
        sys.exit(1)
    os.makedirs(data_root, exist_ok=True)
    log_path = os.path.join(data_root, "log")
    var_path = os.path.join(data_root, "var")
    os.makedirs(log_path, exist_ok=True)
    os.makedirs(var_path, exist_ok=True)

    # === Перманентное состояние runtime-пула (state.yaml, v0.9.6) ===
    # Файл есть → его значения применяются ПОВЕРХ env (файл отражает последнее
    # явное действие пользователя, env — исходную конфигурацию); файла нет →
    # создаётся из текущих env-значений. Идёт ДО _init_multi_backends и до
    # подъёма WEBUI: применённые TARGET-маршрутизация и маппинг моделей должны
    # быть выставлены до старта бэкендов/роутинга. Битый файл не роняет старт —
    # [WARN] + работа на env (см. state_store.apply_on_startup).
    state_store.apply_on_startup()

    # === PID-файл (при ЛЮБОМ запуске, не только в detach) ===
    # Путь — ADAPTER_DATA_ROOT/var + basename(ADAPTER_PIDFILE) (см.
    # daemon._pidfile_path). Без консоли (контейнер, служба) это единственный
    # способ адресовать процесс:
    # kill $(cat "$ADAPTER_DATA_ROOT/var/adapter.pid").
    # Место записи — ДО стартовой проверки бэкендов (сетевой опрос
    # GET /v1/models по каждому бэкенду): файл существует уже во время
    # проверки, поэтому процесс можно найти/остановить, не дожидаясь её.
    # Выше остались только мгновенные FATAL-проверки (пустой
    # ADAPTER_BACKEND_CONFIG, корень-файл) — на них процесс не оставляет
    # файла. Если же проверка бэкендов уронит старт ([FATAL] Failed to
    # initialize backends), файл снимет atexit (зарегистрирован ниже).
    # Существующий файл перезаписывается своим PID (stale-PID не
    # проверяется — в контейнере PID-namespace свой).
    # Удаление: явно в _finish (вежливый выход) + atexit — аварийный выход
    # (sys.exit/необработанное исключение) тоже чистит за собой. Повторный
    # сигнал (os._exit(130)) atexit не выполняет — файл остаётся, это не
    # штатный выход; следующий старт его перезапишет.
    _pidfile = _write_pidfile()
    atexit.register(_remove_pidfile)
    print(f"[PID]       {os.getpid()} → {_pidfile}")

    # === Backend config ===
    _d(f"[INIT] Backend config: {ADAPTER_BACKEND_CONFIG}")
    try:
        _init_multi_backends(ADAPTER_BACKEND_CONFIG)
    except Exception as e:
        print(f"[FATAL] Failed to initialize backends: {e}")
        print("Adapter cannot start. Exiting.")
        sys.exit(1)

    # Стартовый баннер бэкендов (v0.9.8). Значения читаются ЧЕРЕЗ МОДУЛЬ
    # (_config._BACKENDS, а не импортированные по значению имена): коллекции
    # бэкендов/моделей мутируются на месте (_init_multi_backends), но
    # «читаем атрибут модуля» — страховка на случай смены инварианта.
    # Литерал "Backends:" сохранён намеренно: на нём стоит прокси времени
    # в tests/test_manual_check.py (строка печатается ПОСЛЕ опроса бэкендов).
    backends = _config._BACKENDS
    model_counts: Counter[str] = Counter()
    for _bname, _ in _config._MODEL_TO_BACKEND.values():
        model_counts[_bname] += 1
    print(f"Backends:   {len(backends)} configured:")
    if not backends:
        # На живом старте недостижимо: _init_multi_backends делает sys.exit(1)
        # при пустом списке или «ни одной модели» (см. [WARN]/[FATAL] выше).
        print("  (нет настроенных бэкендов — см. [WARN]/[FATAL] выше)")
    for b in backends:
        n_models = model_counts.get(b["name"], 0)
        default = " [default]" if b is _config._DEFAULT_BACKEND else ""
        print(f"  - {b['name']}: {b['base']}  ({n_models} models){default}")
    print(
        f"TARGET:     messages={_config.ADAPTER_MESSAGES_TARGET}  "
        f"completions={_config.ADAPTER_COMPLETIONS_TARGET}  "
        f"responses={_config.ADAPTER_RESPONSES_TARGET}"
    )
    print(f"{'=' * 70}\n")

    # ThreadingHTTPServer вместо socketserver.TCPServer: [CC] может
    # открывать несколько параллельных запросов (конкурентные tool calls),
    # а однопоточный сервер обрабатывает их строго последовательно — пока
    # первый запрос ждёт ADAPTER_TIMEOUT секунд от бэкенда, остальные
    # соединения простаивают в очереди accept() и клиент рвёт их по своему
    # таймауту. Это и есть основной источник BrokenPipeError в логе.
    # Веб-интерфейс WEBUI (webserver.py — общее ядро; эндпойнты:
    # session_viewer.py "/session" + webui_status.py "/" + webui_ops.py
    # health-эндпойнты) поднимается ВСЕГДА — отдельный daemon-поток внутри
    # процесса адаптера. Корень — директория ADAPTER_DATA_ROOT (всегда
    # непуст, дефолт ./tmp/adapter): внутри log/ лежат *.parts папки сессий и
    # .err-файлы, внутри var/ — model-usage.yaml и прочее состояние.
    webui_root = ADAPTER_DATA_ROOT
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
        # Стартовая фоновая проверка бэкендов (опрос списка моделей
        # GET /v1/models): первый GET "/" сразу показывает свежие данные.
        # Дублирует стартовый опрос _init_multi_backends — приемлемо: один
        # раз, фоново, с таймаутом REFRESH_TIMEOUT (10 с на бэкенд), не
        # ADAPTER_TIMEOUT (300 с).
        # Локальные импорты: скрипт не импортирует webui_status; config
        # связан только именами (не модулем).
        from backend_adapter import config as _cfg
        from backend_adapter.webui_status import REFRESH_TIMEOUT

        _cfg.start_refresh(timeout=REFRESH_TIMEOUT)
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
    # SIGINT и SIGTERM перехвачены на KeyboardInterrupt — serve_forever
    # выходит, finally выполняет вежливое завершение. Проблема была в том,
    # что ПОВТОРНЫЙ Ctrl-C во время finally (flush_table пишет YAML) прерывал
    # запись: Python кидает KI в главный поток, где бы тот ни был. Решение
    # (вынесено в backend_adapter/shutdown.py — покрыто тестами, см. ADR):
    # (1) первый сигнал обрабатывает ЕДИНЫЙ хэндлер _first_signal, который
    # сразу переключает SIGINT/SIGTERM на немедленный os._exit(130) и лишь
    # затем делает raise KeyboardInterrupt — повторный/задвоенный сигнал
    # (PyInstaller bootloader форвардит Ctrl-C группе) не может уйти в
    # непойманный KI в микроокне между raise и переустановкой хэндлеров в
    # graceful_shutdown (v0.9.1, баг грязного выхода бинаря); (2) как только
    # начали завершение, SIGINT/SIGTERM переключаются на немедленный
    # os._exit(130) — повторный сигнал не может прервать flush_table, а
    # просто тихо завершает процесс.
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
        # PID-файл — после flush_table: файл снимается последним шагом
        # штатного выхода (идемпотентно; при повторном сигнале сюда не
        # доходим — atexit тоже не срабатывает на os._exit(130)).
        _remove_pidfile()

    try:
        with QuietThreadingHTTPServer((ADAPTER_ENDPOINT_HOST, PROXY_PORT), Adapter) as httpd:
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass  # Ctrl-C / SIGTERM: ниже — вежливое завершение
            finally:
                # Переключение сигналов + остановка слушателей + фоновой проверки
                # + финальный flush usage-таблицы + печать «[EXIT] Bye»
                # (см. shutdown.graceful_shutdown: остановки по отдельности глотают
                # ошибки, процедуру прервать нельзя — повторный сигнал уже =
                # немедленный os._exit(130); маркер «[EXIT] Bye» печатает сам
                # graceful_shutdown — контракт завершения живёт в shutdown.py).
                graceful_shutdown(
                    httpd,
                    webui,
                    exporter,
                    ADAPTER_EXPORTER_ENABLE,
                    _finish,
                )
    except KeyboardInterrupt:
        # Внешний предохранитель (v0.9.1): если KeyboardInterrupt всё же дошёл
        # до этой точки (не должен — единый хэндлер _first_signal с первого
        # сигнала переключает оба сигнала на _force_exit, повторный умирает
        # тихо os._exit(130)), выходим тихо, без traceback. Это последний
        # рубеж против «[EXIT] Bye» + traceback + [PYI-7290] на
        # PyInstaller-бинаре: контракт повторного сигнала (130) сохранён —
        # повторный сигнал и так завершил бы процесс, здесь завершаем сами.
        from backend_adapter.shutdown import os_exit

        os_exit(130)
