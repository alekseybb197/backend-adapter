#!/usr/bin/env python3
"""model_usage.py — таблица использованных моделей (WIP v0.8.4).

Что делает: каждый входящий запрос агента (BODY Anthropic-формата) несёт
имя модели; после прохождения строгой проверки по списку допустимых моделей
(ADAPTER_STRICT_MODELS / _AVAILABLE_MODELS) модель заносится в таблицу _TABLE.
При ПЕРВОМ обращении к модели выполняется синхронная дымовая проба
4 известных API-эндпоинтов ИМЕННО этой моделью (completions/messages/
responses/embeddings) — результат (found ⇔ HTTP 200) хранится в строке
таблицы. Повторные обращения к уже внесённой модели никогда не
перепроверяют (только инкремент счётчика вызовов). Помимо вызовов строка
накапливает байты трафика обмена с бэкендом (add_usage_bytes: тела запросов
к бэкенду и его ответов, включая повторные попытки). Состояние таблицы
выводится секцией «Использованные модели» на статус-странице WEBUI "/"
(см. webui_status._usage_rows_html).

Персистентность: таблица сохраняется в YAML-файл `model-usage.yaml` в корне
WEBUI (формула `ADAPTER_DEBUG_LOGPATH or "./tmp/webui"`, та же, что у корня
веб-сервера) и при старте загружается из него, если файл есть — счётчики
переживают перезапуски адаптера. Сохранение «грязной» таблицы — не чаще
раза в config.ADAPTER_MODEL_USAGE_SAVE_INTERVAL (сек); создание новой строки
модели, сброс строки и завершение работы адаптера (в т.ч. Ctrl-C —
flush_table в backend-adapter.py) сохраняют сразу. Строки, у которых
`probing: True` (идёт первая синхронная проба), на диск не попадают —
крах в это время теряет только саму новую строку. При жёстком kill потеря
хвоста ≤ периода сохранения. Файл несёт версию формата (`version: 1`).

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
refresh_models). Персистентность (YAML-файл) работает независимо от
мастер-флага. В RUNTIME_CONFIG_POOL таблица не входит.
"""

import os
import threading
import time

import yaml

from . import config

# Жёсткий таймаут одного POST пробы (как PROBE_TIMEOUT у webui_status): при
# недоступном бэкенде первый запрос новой модели ждёт до 4×10 = 40 с.
MODEL_USAGE_PROBE_TIMEOUT = 10.0

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

# Персистентность (YAML-файл в корне WEBUI). _PERSIST_PATH:
#   None (дефолт) → авто-формула (прод): config.ADAPTER_DEBUG_LOGPATH or "./tmp/webui"
#   ""            → выключено (тесты, никакого файлового I/O)
#   иначе         → явный путь к YAML-файлу (standalone webserver, тесты на tmp_path)
# _PERSIST_LOCK сериализует запись файла; _TABLE_LOCK защищает таблицу.
MODEL_USAGE_FILE = "model-usage.yaml"  # имя файла в корне WEBUI
_PERSIST_PATH: str | None = None
_PERSIST_LOCK = threading.Lock()
_LOADED = False  # файл уже пытались загрузить
_DIRTY = False  # есть несохранённые мутации таблицы
_LAST_SAVE = 0.0  # time.time() последнего сохранения
_FORMAT_VERSION = 1  # версия формата YAML-файла (поле version)


# ==================== ПУБЛИЧНЫЙ API ====================


def set_persist_path(path: str | None) -> None:
    """Задать точку хранения таблицы: "" — выключено (тесты), None — авто
    (прод, формула корня WEBUI), иначе — явный путь к YAML-файлу.
    Сбрасывает _LOADED, чтобы следующее обращение перечитало файл по новому
    пути — но только если таблица пуста (после старта источник правды —
    память; файл читается лишь один раз при первом обращении). serve() зовёт
    один раз при старте, до первого обращения."""
    global _PERSIST_PATH, _LOADED
    with _TABLE_LOCK:
        _PERSIST_PATH = path
        _LOADED = False


def usage_persist_file() -> str | None:
    """Эффективный путь к YAML-файлу (для подписи на странице) или None,
    если персистентность выключена ("") — только тесты."""
    if _PERSIST_PATH == "":
        return None
    if _PERSIST_PATH:
        return _PERSIST_PATH
    return _default_root_file()


def flush_table() -> None:
    """Принудительно сохранить таблицу, если есть несохранённые мутации
    (вызывается при завершении работы адаптера, в т.ч. Ctrl-C)."""
    with _TABLE_LOCK:
        dirty = _DIRTY
    if dirty:
        _save_table(force=True)


def record_model_usage(client_model: str) -> None:
    """Точка учёта из server.do_POST (после strict-проверки, до маппинга).

    Всегда: инкремент счётчика существующей строки ИЛИ создание новой.
    При ПЕРВОМ обращении всегда резолвится бэкенд (имя — для колонки
    «Бэкенд»; без сети) и, если config.ADAPTER_MODEL_USAGE_ENABLE, —
    синхронная дымовая проба 4 эндпоинтов resolved-именем модели.
    Повторные обращения никогда не перепроверяют. Не бросает исключений
    наружу: учёт не должен влиять на запрос.
    """
    global _DIRTY
    now = time.strftime("%H:%M:%S")
    # --- Короткая критическая секция: поиск/создание/инкремент ---
    with _TABLE_LOCK:
        _ensure_loaded_locked()
        row = _TABLE.get(client_model)
        if row is not None:
            row["calls"] += 1
            _DIRTY = True
            increment = True
            created = False
        else:
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
            increment = False
            created = True
        need_probe = config.ADAPTER_MODEL_USAGE_ENABLE

    if increment:
        # Инкремент существующей строки: периодическое сохранение «грязной»
        # таблицы (после пробы строка при создании сохранится принудительно).
        _save_table(force=False)
        return

    # --- Вне лока: резолв бэкенда (всегда, без сети — имя для колонки
    # «Бэкенд») и при первом обращении синхронная проба (до 4×10 с) ---
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
                _DIRTY = True
    # Создание строки завершено (probing=False, endpoints на месте) — новая
    # модель сохраняется сразу, независимо от периода.
    if created:
        _save_table(force=True)
    else:
        _save_table(force=False)


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
    global _DIRTY
    if not config.ADAPTER_MODEL_USAGE_ENABLE:
        return
    if sent <= 0 and recv <= 0:
        return
    with _TABLE_LOCK:
        _ensure_loaded_locked()
        row = _TABLE.get(client_model)
        if row is None:
            return
        row["bytes_sent"] += sent
        row["bytes_recv"] += recv
        _DIRTY = True
    _save_table(force=False)


def usage_snapshot() -> list[dict]:
    """Копия строк таблицы в порядке первого обращения (для webui_status).

    Возвращает копии (включая endpoints/errors) — мутация результата не
    затрагивает таблицу. Первый вызов после старта (или смены persist-пути)
    загружает таблицу из YAML-файла — GET "/" после рестарта сразу показывает
    прошлые сессии без новых запросов."""
    with _TABLE_LOCK:
        _ensure_loaded_locked()
        rows = [
            {
                **r,
                "endpoints": dict(r["endpoints"]),
                "errors": dict(r["errors"]),
            }
            for r in _TABLE.values()
        ]
    _save_table(force=False)  # просмотр страницы флашит «грязный» хвост
    return rows


def reset_model_usage() -> None:
    """Очистить таблицу и сбросить состояние персистентности (тесты/isolate_logs).

    YAML-файл НЕ трогает: очистка памяти — для изоляции тестов; файл
    перезапишется следующим сохранением. Полного сброса счётчиков через API
    нет — только per-model reset_model."""
    global _LOADED, _DIRTY, _LAST_SAVE
    with _TABLE_LOCK:
        _TABLE.clear()
        _LOADED = False
        _DIRTY = False
        _LAST_SAVE = 0.0


def reset_model(model: str) -> bool:
    """Удалить строку модели из таблицы и из YAML-файла.

    Возвращает True, если строка существовала (в памяти или в файле).
    Точка вызова — ModelUsageResetEndpoint (POST /api/model-usage/reset)."""
    global _DIRTY
    with _TABLE_LOCK:
        _ensure_loaded_locked()
        existed = model in _TABLE
        if existed:
            del _TABLE[model]
            _DIRTY = True
    if existed:
        _save_table(force=True)
    return existed


# ==================== ПЕРСИСТЕНТНОСТЬ (YAML) ====================


def _default_root() -> str:
    """Корень WEBUI — та же формула, что у webui_root в backend-adapter.py
    (ADAPTER_DEBUG_LOGPATH or "./tmp/webui"). Читается лениво (живое чтение
    config.ADAPTER_DEBUG_LOGPATH — его могут менять тесты между вызовами)."""
    return config.ADAPTER_DEBUG_LOGPATH or "./tmp/webui"


def _default_root_file() -> str:
    return os.path.join(_default_root(), MODEL_USAGE_FILE)


def _effective_persist_file() -> str:
    if _PERSIST_PATH:
        return _PERSIST_PATH
    return _default_root_file()


def _ensure_loaded_locked() -> None:
    """Ленивая загрузка таблицы из YAML-файла. Вызывается ВНУТРИ _TABLE_LOCK
    в начале record_model_usage / add_usage_bytes / usage_snapshot /
    reset_model (единая точка). Пропускается, если файл уже читали, таблица
    непуста (продолжение работы в рамках процесса) или персистентность
    выключена (""). Повреждённый/битый файл — таблица стартует пустой, файл
    НЕ трогаем (перезапишется первым сохранением)."""
    global _LOADED
    if _LOADED or _TABLE or _PERSIST_PATH == "":
        return
    try:
        rows = _read_persist_file(_effective_persist_file())
        for name, raw in rows.items():
            norm = _normalize_row(name, raw)
            if norm is not None:
                _TABLE[norm["model"]] = norm
    except Exception as e:  # noqa: BLE001 — загрузка не должна ронять учёт
        _log("persist", f"ignoring unreadable file: {e}")
    finally:
        _LOADED = True


def _read_persist_file(path: str) -> dict:
    """Прочитать YAML и вернуть {имя модели: сырая строка}. Поддерживает
    текущий формат ({"version": 1, "models": {...}}) и плоский легаси
    (строки прямо в корне, без version/models). Ошибки не бросает наружу:
    не-dict/битый YAML → пусто (обрабатывается в _ensure_loaded_locked)."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return {}
    version = data.get("version")
    if version is not None and version != _FORMAT_VERSION:
        # Незнакомая версия формата: файл игнорируется (читатель принимает
        # только известные версии), будет перезаписан первым сохранением.
        return {}
    raw_models = data.get("models")
    if isinstance(raw_models, dict):
        return raw_models
    # Плоский легаси: без ключа "models" корень сам по себе — карта строк.
    models = {k: v for k, v in data.items() if k != "version"}
    if models:
        return models
    return {}


def _normalize_row(model: str, raw: object) -> dict | None:
    """Привести строку из YAML к внутренней схеме; None — строка отбрасывается.

    Нормализация: имя — str(raw["model"] or model); счётчики и байты —
    неотрицательные int (иначе 0); endpoints — только dict, per-path
    status int|None и found bool (по status == 200, если нет), незнакомые
    pname отбрасываются (рендер ходит по config.ENDPOINT_PROBES с .get());
    errors — dict[str, str]; first_seen — строка HH:MM:SS (иначе текущее
    время); probing — всегда False (файл хранит только завершённые строки);
    неизвестные ключи отбрасываются."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("model", model) or model)

    def _to_int(v: object) -> int:
        # Только реально приводимые к int типы (числа и строки): yaml-значения
        # из файла могут быть чем угодно — от битых строк до списков.
        if v is None:
            return 0
        if isinstance(v, bool):
            v = int(v)
        elif not isinstance(v, (int, float, str)):
            return 0
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0

    endpoints: dict[str, dict] = {}
    raw_eps = raw.get("endpoints")
    if isinstance(raw_eps, dict):
        known = {pname for pname, _path, _tpl in config.ENDPOINT_PROBES}
        for pname, ep in raw_eps.items():
            if pname not in known or not isinstance(ep, dict):
                continue
            status = ep.get("status")
            if status is not None:
                try:
                    status = int(status)
                except (TypeError, ValueError):
                    status = None
            endpoints[str(pname)] = {
                "status": status,
                "found": bool(ep.get("found", status == 200)),
            }

    errors: dict[str, str] = {}
    raw_errors = raw.get("errors")
    if isinstance(raw_errors, dict):
        errors = {str(k): str(v) for k, v in raw_errors.items()}

    first_seen = raw.get("first_seen")
    if not isinstance(first_seen, str) or not first_seen:
        first_seen = time.strftime("%H:%M:%S")

    return {
        "model": name,
        "backend": str(raw.get("backend", "") or ""),
        "calls": _to_int(raw.get("calls")),
        "bytes_sent": _to_int(raw.get("bytes_sent")),
        "bytes_recv": _to_int(raw.get("bytes_recv")),
        "endpoints": endpoints,
        "errors": errors,
        "first_seen": first_seen,
        "probing": False,
    }


def _serialize_table() -> dict:
    """Снимок таблицы для записи: {"version": 1, "models": {...}}.
    Строки с probing=True в файл не попадают (в dump-копии probing
    принудительно False): на диск — только завершённые строки."""
    with _TABLE_LOCK:
        models = {}
        for m, r in _TABLE.items():
            if r.get("probing"):
                continue
            models[m] = {
                "model": r["model"],
                "backend": r["backend"],
                "calls": r["calls"],
                "bytes_sent": r["bytes_sent"],
                "bytes_recv": r["bytes_recv"],
                "endpoints": {p: dict(ep) for p, ep in r["endpoints"].items()},
                "errors": dict(r["errors"]),
                "first_seen": r["first_seen"],
                "probing": False,
            }
    return {"version": _FORMAT_VERSION, "models": models}


def _atomic_write_yaml(path: str, payload: dict) -> None:
    """Атомарная запись YAML: временный файл в той же директории + os.replace.
    os.makedirs создаёт корень при необходимости. OSError НЕ ловится здесь —
    его обрабатывает _save_table (учёт не должен ронять запрос)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            payload,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    os.replace(tmp_path, path)


def _save_table(force: bool) -> None:
    """Сохранить таблицу в YAML. force=True — независимо от периода; иначе —
    только если есть грязные мутации и прошёл период. Пишет полный снимок
    атомарно (tmp + os.replace); OSError — лог, учёт не роняет."""
    if _PERSIST_PATH == "":
        return
    global _DIRTY, _LAST_SAVE
    with _PERSIST_LOCK:
        now = time.time()
        if not force and not (
            _DIRTY and now - _LAST_SAVE >= config.ADAPTER_MODEL_USAGE_SAVE_INTERVAL
        ):
            return
        payload = _serialize_table()
        try:
            _atomic_write_yaml(_effective_persist_file(), payload)
        except OSError as e:
            _log("persist", f"failed to save {_effective_persist_file()}: {e}")
            return
        _LAST_SAVE = now
        _DIRTY = False


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
    "MODEL_USAGE_FILE",
    "MODEL_USAGE_PROBE_TIMEOUT",
    "set_persist_path",
    "usage_persist_file",
    "flush_table",
    "record_model_usage",
    "add_usage_bytes",
    "usage_snapshot",
    "reset_model_usage",
    "reset_model",
]
