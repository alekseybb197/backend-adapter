#!/usr/bin/env python3
"""model_usage.py — таблица использованных моделей (WIP v0.8.4).

Что делает: каждый входящий запрос агента (BODY Anthropic-формата) несёт
имя модели; после прохождения строгой проверки по списку допустимых моделей
(ADAPTER_STRICT_MODELS / _AVAILABLE_MODELS) модель заносится в in-memory
таблицу _TABLE. При ПЕРВОМ обращении к модели выполняется синхронная дымовая
проба 4 известных API-эндпоинтов ИМЕННО этой моделью (completions/messages/
responses/embeddings) — результат (found ⇔ HTTP 200) хранится в строке
таблицы. Повторные обращения к уже внесённой модели никогда не
перепроверяют (только инкремент счётчика вызовов). Помимо вызовов строка
накапливает байты трафика обмена с бэкендом (add_usage_bytes: тела запросов
к бэкенду и его ответов, включая повторные попытки). Состояние таблицы
выводится секцией «Использованные модели» на статус-странице WEBUI "/"
(см. webui_status._usage_rows_html).

Точка учёта — server.do_POST: model_usage.record_model_usage(client_model)
сразу после strict-проверки и ДО модельного маппинга — имя из BODY
(клиентское, как в _AVAILABLE_MODELS). Недопустимая модель (HTTP 400) в
таблицу не попадает (хук стоит после return). Провал пробы/резолва никогда
не роняет запрос: все ошибки ловятся внутри и пишутся в строку/лог.

Синхронность: проба выполняется в потоке запроса (первый запрос новой
модели ждёт до 4×MODEL_USAGE_PROBE_TIMEOUT); выполняется ВНЕ _TABLE_LOCK
(короткая критическая секция — только поиск/создание/инкремент), поэтому
два одновременных первых обращения к ОДНОЙ модели дают одну пробу (второй
поток видит существующую строку), а к РАЗНЫМ моделям — пробы идут
параллельно в потоках ThreadingHTTPServer.

Мастер-флаг — ADAPTER_MODEL_USAGE_ENABLE (config.py, дефолт 1): 0 — пробы
отключены, учёт обращений остаётся. Пробы по модели НЕ зависят от
ADAPTER_ENDPOINT_PROBE (тот управляет только фоновой пробой бэкендов в
refresh_models). Таблица живёт в памяти процесса (сброс при старте = пустой
модульный глобал) и в RUNTIME_CONFIG_POOL не входит.
"""

import threading
import time

from . import config

# Жёсткий таймаут одного POST пробы (как PROBE_TIMEOUT у webui_status): при
# недоступном бэкенде первый запрос новой модели ждёт до 4×5 = 20 с.
MODEL_USAGE_PROBE_TIMEOUT = 5.0

# Таблица использованных моделей: client_model → строка.
# Строка:
#   {"model": str,            # client_model (ключ == поле, для webui)
#    "backend": str,          # имя бэкенда из config._resolve_backend
#    "calls": int,            # счётчик обращений (растёт при каждом запросе)
#    "bytes_sent": int,       # байты тел запросов → бэкенду (все попытки)
#    "bytes_recv": int,       # байты тел ответов ← бэкенда (все попытки,
#                             #   включая тела ошибок)
#    "endpoints": {pname: {"status": int|None, "found": bool}},
#                             # только реально пробованные пути; found ⇔ HTTP 200
#    "errors": {pname: текст} # сетевые ошибки пробы
#    "first_seen": str,       # "HH:MM:SS" первого обращения
#    "probing": bool}         # True, пока первый запрос выполняет синхронную пробу
_TABLE: dict[str, dict] = {}
_TABLE_LOCK = threading.Lock()


# ==================== ПУБЛИЧНЫЙ API ====================


def record_model_usage(client_model: str) -> None:
    """Точка учёта из server.do_POST (после strict-проверки, до маппинга).

    Всегда: инкремент счётчика существующей строки ИЛИ создание новой.
    При ПЕРВОМ обращении всегда резолвится бэкенд (имя — для колонки
    «Бэкенд»; без сети) и, если config.ADAPTER_MODEL_USAGE_ENABLE, —
    синхронная дымовая проба 4 эндпоинтов resolved-именем модели.
    Повторные обращения никогда не перепроверяют. Не бросает исключений
    наружу: учёт не должен влиять на запрос.
    """
    now = time.strftime("%H:%M:%S")
    # --- Короткая критическая секция: поиск/создание/инкремент ---
    with _TABLE_LOCK:
        row = _TABLE.get(client_model)
        if row is not None:
            row["calls"] += 1
            return
        _TABLE[client_model] = {
            "model": client_model,
            "backend": "",
            "calls": 1,
            "bytes_sent": 0,
            "bytes_recv": 0,
            "endpoints": {},
            "errors": {},
            "first_seen": now,
            "probing": True,
        }
        need_probe = config.ADAPTER_MODEL_USAGE_ENABLE

    # --- Вне лока: резолв бэкенда (всегда, без сети — имя для колонки
    # «Бэкенд») и при первом обращении синхронная проба (до 4×5 с) ---
    result = None
    backend_name = ""
    try:
        backend_cfg, resolved = config._resolve_backend(client_model)
        backend_name = backend_cfg["name"]
        if need_probe:
            if _backend_has_model(backend_cfg, resolved):
                result = _probe_model_endpoints(backend_cfg, resolved)
                _log_probe(client_model, backend_name, result)
            else:
                # Модели нет среди моделей бэкенда в /v1/models — пробовать
                # нечем (как с probe-моделью в config.probe_endpoints):
                # колонки эндпоинтов остаются «—».
                _log(
                    client_model,
                    f"probe skipped: model '{resolved}' not among backend '{backend_name}' models",
                )
    except Exception as e:  # noqa: BLE001 — учёт не должен валить запрос
        _log(client_model, f"usage probe failed: {e}")
    finally:
        with _TABLE_LOCK:
            cur = _TABLE.get(client_model)
            if cur is not None:
                cur["backend"] = backend_name
                if result is not None:
                    cur["endpoints"] = result["endpoints"]
                    cur["errors"] = result["errors"]
                cur["probing"] = False


def add_usage_bytes(client_model: str, sent: int, recv: int) -> None:
    """Накопить байты обмена с бэкендом (тело запроса + тело ответа).

    Точка вызова — do_POST (фиксация в finally на любой исход запроса).
    Строка к этому моменту уже существует (record_model_usage вызывается
    раньше, до сетевых попыток); строки нет — пропускаем (no-op): защита от
    будущих точек вызова вне основного пути. Служебные дымовые пробы
    эндпоинтов сюда НЕ попадают (другой путь кода — config.probe_endpoints /
    _probe_model_endpoints), т.е. счётчики = только запросы агента.

    Гейт: config.ADAPTER_MODEL_USAGE_ENABLE (живое чтение, как в
    record_model_usage). Не бросает исключений: учёт не должен влиять на
    запрос. Значения неотрицательные, фактические байты (python-int не
    переполняется — «крышки» не нужны)."""
    if not config.ADAPTER_MODEL_USAGE_ENABLE:
        return
    if sent <= 0 and recv <= 0:
        return
    with _TABLE_LOCK:
        row = _TABLE.get(client_model)
        if row is None:
            return
        row["bytes_sent"] += sent
        row["bytes_recv"] += recv


def usage_snapshot() -> list[dict]:
    """Копия строк таблицы в порядке первого обращения (для webui_status).

    Возвращает копии (включая endpoints/errors) — мутация результата не
    затрагивает таблицу."""
    with _TABLE_LOCK:
        return [
            {
                **r,
                "endpoints": dict(r["endpoints"]),
                "errors": dict(r["errors"]),
            }
            for r in _TABLE.values()
        ]


def reset_model_usage() -> None:
    """Очистить таблицу (используется тестами и isolate_logs)."""
    with _TABLE_LOCK:
        _TABLE.clear()


# ==================== ПРИВАТНОЕ ====================


def _backend_has_model(backend_cfg: dict, resolved: str) -> bool:
    """Есть ли resolved среди моделей бэкенда backend_cfg в _MODEL_TO_BACKEND.

    Ложь — пробу не делаем (модели у бэкенда нет; колонки эндпоинтов «—»).
    Читаем через config._MODEL_TO_BACKEND (живой атрибут): словарь может
    переприсваиваться целиком (тесты/refresh), ссылка на импорт устарела бы."""
    return any(
        config._MODEL_TO_BACKEND[mid][0] == backend_cfg["name"] and mid == resolved
        for mid in config._MODEL_TO_BACKEND
    )


def _probe_model_endpoints(backend_cfg: dict, resolved: str) -> dict:
    """Синхронная проба 4 эндпоинтов модели у бэкенда (вызывается вне лока).

    Собирает probes {путь: resolved} на все config.ENDPOINT_PROBES и зовёт
    низкоуровневую config._probe_backend_endpoints (та же классификация:
    found ⇔ HTTP 200, сеть/таймаут — found=False + текст в errors). Возвращает
    нормализованный к схеме записи результат:
      {"endpoints": {pname: {"status": int|None, "found": bool}}, "errors": {...}}
    — ключи по коротким именам ENDPOINT_PROBES (не пути), чтобы webui рендерил
    колонки без нормализации. Отдельная функция: её мокают тесты (в т.ч.
    _setup_adapter в test_server), не трогая config."""
    probes = {path: resolved for _pname, path, _tpl in config.ENDPOINT_PROBES}
    result = config._probe_backend_endpoints(backend_cfg, probes, timeout=MODEL_USAGE_PROBE_TIMEOUT)
    endpoints = {}
    for pname, path, _tpl in config.ENDPOINT_PROBES:
        if path in result["endpoints"]:
            endpoints[pname] = result["endpoints"][path]
    return {"endpoints": endpoints, "errors": dict(result["errors"])}


def _log(client_model: str, msg: str) -> None:
    """Консольный лог строки [MODEL_USAGE]; гейт — ADAPTER_DEBUG (живое
    чтение, как _log_probe в config.py: config/model_usage не импортируют
    logger — корень DAG)."""
    if config.ADAPTER_DEBUG:
        print(f"[MODEL_USAGE] model={client_model!r}: {msg}")


def _log_probe(client_model: str, backend_name: str, result: dict) -> None:
    """Лог-блок [MODEL_USAGE] первой пробы модели: сырые HTTP-коды по
    ENDPOINT_PROBES (единый формат с config._log_probe). Инкременты повторных
    обращений не логируются (спам при каждом запросе — как кэш-хиты пробы)."""
    if not config.ADAPTER_DEBUG:
        return
    errors = result["errors"]
    if errors:
        _log(client_model, f"backend '{backend_name}': failed: {errors}")
        return
    parts = []
    for pname, _path, _tpl in config.ENDPOINT_PROBES:
        ep = result["endpoints"].get(pname)
        if ep is None:
            continue
        status = ep["status"]
        parts.append(f"{pname}={status if status is not None else 'err'}")
    if parts:
        _log(client_model, f"backend '{backend_name}': {' '.join(parts)}")


__all__ = [
    "MODEL_USAGE_PROBE_TIMEOUT",
    "record_model_usage",
    "add_usage_bytes",
    "usage_snapshot",
    "reset_model_usage",
]
