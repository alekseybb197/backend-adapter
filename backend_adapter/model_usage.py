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
накапливает токены из usage-блоков ответов бэкенда (add_usage_tokens:
input_tokens из usage.prompt_tokens, output_tokens из usage.completion_tokens;
ответы без usage — ошибки, обрывы, бэкенд без usage-поддержки — токенов
не дают). Состояние таблицы выводится секцией «Использованные модели» на
статус-странице WEBUI "/" (см. webui_status._usage_rows_html).

Персистентность: таблица сохраняется в YAML-файл `model-usage.yaml` в корне
WEBUI (корень — директория `ADAPTER_DEBUG_LOGPATH`, дефолт ./tmp/logs;
та же формула, что у корня веб-сервера) и при старте загружается из него,
если файл есть — счётчики
переживают перезапуски адаптера. Сохранение «грязной» таблицы — не чаще
раза в config.ADAPTER_MODEL_USAGE_SAVE_INTERVAL (сек); создание новой строки
модели, обнуление счётчиков строки (reset_model, кнопка «Сбросить») и
завершение работы адаптера (в т.ч. Ctrl-C —
flush_table в backend-adapter.py) сохраняют сразу. Строки, у которых
`probing: True` (идёт первая синхронная проба), на диск не попадают —
крах в это время теряет только саму новую строку. При жёстком kill потеря
хвоста ≤ периода сохранения. Файл несёт версию формата (`version: 2`); файлы
версии 1 (байтовые счётчики `bytes_sent`/`bytes_recv`) при загрузке
мигрируются: calls/backend/endpoints/errors/first_seen сохраняются, байты
отбрасываются, токены стартуют с 0 (следующее сохранение пишет v2).

«Сбросить» (reset_model, POST /api/model-usage/reset) ОБНУЛЯЕТ счётчики
строки (calls/input_tokens/output_tokens → 0), а не удаляет её: строка с
результатами пробы эндпоинтов остаётся, повторные обращения не делают новую
пробу (fast-path).

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

Синхронизация эндпоинтов: found-результаты проб МОДЕЛИ (при первом
обращении, «Перепроверить» строки и загрузке model-usage.yaml) переносятся
в config._ENDPOINT_STATE бэкенда (config.upsert_endpoint_state) — колонка
«Доступные API» бэкенда на статус-странице и экспортёр видят эндпоинт,
найденный моделью, даже если фоновая проверка бэкенда его не пробовала.
Переносятся только found=True и только для бэкендов из числа настроенных
(config._BACKEND_BY_NAME): эндпоинты осиротевших бэкендов (удалены из YAML)
в кэш не добавляются. Сети при синхронизации нет — переносится уже добытый
результат.

Тарифы моделей (колонка Cost таблицы Models in use): YAML-файл по пути
config.ADAPTER_MODELS_TARIFFS читается с диска при загрузке накопленных
счётчиков (см. _ensure_loaded_locked) и при добавлении НОВОЙ модели в
таблицу (см. record_model_usage); lookup_tariff() — без сети и диска.
Рендер Cost — в webui_status (см. _fmt_cost / _usage_rows_html): стоимость
считается на лету из токенов строки по тарифу на момент отображения.
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
#    "input_tokens": int,     # токены из usage.prompt_tokens ответов бэкенда
#    "output_tokens": int,    # токены из usage.completion_tokens ответов
#                             #   бэкенда (ответы без usage токенов не дают)
#    "endpoints": {pname: {"status": int|None, "found": bool}},
#                             # только реально пробованные пути; found ⇔ HTTP 200
#    "errors": {pname: текст} # сетевые ошибки пробы
#    "first_seen": str,       # "HH:MM:SS" первого обращения
#    "probing": bool}         # True, пока первый запрос выполняет синхронную пробу
_TABLE: dict[str, dict] = {}
_TABLE_LOCK = threading.Lock()

# Тарифы моделей для колонки Cost таблицы Models in use. Источник — YAML-файл
# по пути config.ADAPTER_MODELS_TARIFFS (см. config.py; пусто — тарифов нет,
# колонка Cost показывает «--»). Запись нормализованного тарифа:
#   {"input_price": float, "output_price": float, "currency": str,
#    "price_per": float}   # price_per >= 1 (0/None в файле → 1)
# Ключ поиска — (name, backend), где backend может быть "" — тариф-«wildcard»
# без поля backend матчит любой бэкенд; тариф с backend — только свой.
# _TARIFFS не мутируется после загрузки (заменяется целиком под _TARIFF_LOCK);
# «перечитывание» = повторный вызов _load_tariffs_locked с диска (маленький
# файл, редкое событие — см. точки вызова в _ensure_loaded_locked и
# record_model_usage).
_TARIFFS: dict[tuple[str, str], dict] = {}
_TARIFF_LOCK = threading.Lock()

# Персистентность (YAML-файл в корне WEBUI). _PERSIST_PATH:
#   None (дефолт) → авто-формула (прод): config.ADAPTER_DEBUG_LOGPATH (всегда непуст)
#   ""            → выключено (тесты, никакого файлового I/O)
#   иначе         → явный путь к YAML-файлу (standalone webserver, тесты на tmp_path)
# _PERSIST_LOCK сериализует запись файла; _TABLE_LOCK защищает таблицу.
MODEL_USAGE_FILE = "model-usage.yaml"  # имя файла в корне WEBUI
_PERSIST_PATH: str | None = None
_PERSIST_LOCK = threading.Lock()
_LOADED = False  # файл уже пытались загрузить
_DIRTY = False  # есть несохранённые мутации таблицы
_LAST_SAVE = 0.0  # time.time() последнего сохранения
_FORMAT_VERSION = 2  # версия формата YAML-файла (поле version)

# Перепроверка строки модели («Перепроверить»): фоновый снимок идущей пробы.
# В таблицу НЕ пишется (в отличие от probing первичной пробы — reprobe не
# блокирует сериализацию строки: файл принудительно сохраняется в finally
# воркера, и строка в нём должна остаться на всё время пробы).
_REPROBE: dict[str, dict] | None = None  # client_model → {"started_at": float}
_REPROBE_LOCK = threading.Lock()


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
    (вызывается при завершении работы адаптера, в т.ч. Ctrl-C).

    Устойчив к повторному Ctrl-C: если запись YAML прервана сигналом
    посреди _atomic_write_yaml, _save_table(force=True) завершается
    исключением KeyboardInterrupt и _DIRTY остаётся True (см. _save_table) —
    здесь мы гасим прерывание и даём таблице уйти в память (учёт не должен
    ронять завершение адаптера; файл перепишется следующим сохранением).
    """
    try:
        with _TABLE_LOCK:
            dirty = _DIRTY
        if dirty:
            _save_table(force=True)
    except KeyboardInterrupt:
        # Повторный Ctrl-C пришёл в момент файловой записи: не даём ему
        # уронить завершение процесса поверх (см. переключение SIGINT на
        # os._exit в finally backend-adapter.py). Сохранение не удалось —
        # файл останется прежним, таблица жива в памяти до выхода процесса.
        pass


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
                "input_tokens": 0,
                "output_tokens": 0,
                "endpoints": {},
                "errors": {},
                "first_seen": now,
                "probing": True,
            }
            increment = False
            created = True
        need_probe = config.ADAPTER_MODEL_USAGE_ENABLE

    if created:
        # Добавление НОВОЙ модели — точка перечитывания тарифов (см. контракт
        # в config.ADAPTER_MODELS_TARIFFS): файл маленький, событие редкое —
        # повторное чтение с диска дёшево, а стоимость новой модели всегда
        # считается по актуальному тарифу.
        with _TARIFF_LOCK:
            _load_tariffs_locked()

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
                if result is not None:
                    # Синхронизация с фоновой проверкой бэкендов: найденные
                    # пробой модели эндпоинты видны колонке «Доступные API»
                    # бэкенда (см. _sync_found_endpoints).
                    _sync_found_endpoints(backend_name, result)
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


def add_usage_tokens(client_model: str, input_tokens: int, output_tokens: int) -> None:
    """Накопить токены из usage-блоков ответов бэкенда.

    input_tokens — usage.prompt_tokens, output_tokens — usage.completion_tokens
    (сервер берёт их из ответа бэкенда; для стрима — из финального usage-
    чанка). Ответы без usage (ошибки, обрывы, бэкенд без usage-поддержки)
    токенов не дают: вызывающий передаёт 0/0, счётчики не трогаются.

    Точка вызова — do_POST (фиксация в finally на любой исход запроса).
    Строка к этому моменту уже существует (record_model_usage вызывается
    раньше, до сетевых попыток); строки нет — пропускаем (no-op): защита от
    будущих точек вызова вне основного пути. Служебные дымовые пробы
    эндпоинтов сюда НЕ попадают (другой путь кода — config.probe_endpoints /
    _probe_model_endpoints), т.е. счётчики = только запросы агента. Пробы
    usage-блока не возвращают вовсе — токенов и не дают.

    Гейт: config.ADAPTER_MODEL_USAGE_ENABLE (живое чтение, как в
    record_model_usage). Не бросает исключений: учёт не должен влиять на
    запрос. Значения неотрицательные; 0 и в input, и в output — нет токенов
    (не мутируем; гейт по сумме, а не по одному полю — input=0 при
    output>0 валиден)."""
    global _DIRTY
    if not config.ADAPTER_MODEL_USAGE_ENABLE:
        return
    if input_tokens <= 0 and output_tokens <= 0:
        return
    with _TABLE_LOCK:
        _ensure_loaded_locked()
        row = _TABLE.get(client_model)
        if row is None:
            return
        row["input_tokens"] += input_tokens
        row["output_tokens"] += output_tokens
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
    нет — только per-model reset_model (обнуление счётчиков строки)."""
    global _LOADED, _DIRTY, _LAST_SAVE
    with _TABLE_LOCK:
        _TABLE.clear()
        _LOADED = False
        _DIRTY = False
        _LAST_SAVE = 0.0


def reset_model(model: str) -> bool:
    """Обнулить счётчики строки модели: calls/input_tokens/output_tokens → 0.

    Строка (модель/бэкенд/endpoints/errors/first_seen) ОСТАЁТСЯ в таблице и
    в YAML-файле — «Сбросить» очищает накопленные счётчики, а не удаляет
    модель (для удаления строки полного сброса таблицы нет). Повторные
    обращения к модели не перепроверяют эндпоинты (строка существует —
    fast-path). Возвращает True, если строка существовала (в памяти или в
    файле). Точка вызова — ModelUsageResetEndpoint (POST
    /api/model-usage/reset)."""
    global _DIRTY
    with _TABLE_LOCK:
        _ensure_loaded_locked()
        existed = model in _TABLE
        if existed:
            row = _TABLE[model]
            row["calls"] = 0
            row["input_tokens"] = 0
            row["output_tokens"] = 0
            _DIRTY = True
    if existed:
        _save_table(force=True)
    return existed


def start_reprobe(client_model: str) -> bool:
    """Запустить фоновую перепроверку эндпоинтов строки модели.

    Возвращает True, если перепроверка запущена этим вызовом; False — строки
    нет в таблице, у строки идёт первая (первичная) проба (probing=True) или
    перепроверка этой модели уже выполняется. Поток — daemon. Точка вызова —
    ModelUsageReprobeEndpoint (POST /api/model-usage/reprobe)."""
    global _REPROBE
    with _TABLE_LOCK:
        _ensure_loaded_locked()
        row = _TABLE.get(client_model)
        if row is None or row.get("probing"):
            return False
    with _REPROBE_LOCK:
        if _REPROBE is not None:
            return False
        _REPROBE = {client_model: {"started_at": time.time()}}
    t = threading.Thread(
        target=_reprobe_worker, args=(client_model,), name="model-usage-reprobe", daemon=True
    )
    t.start()
    return True


def reprobe_state() -> dict:
    """Снимок перепроверки: {"running": bool, "model": str|None,
    "started_at": float|None}. Для /api/model-usage/reprobe-state и баннера."""
    with _REPROBE_LOCK:
        if _REPROBE is None:
            return {"running": False, "model": None, "started_at": None}
        ((model, info),) = _REPROBE.items()
        return {"running": True, "model": model, "started_at": info["started_at"]}


def _reprobe_worker(client_model: str) -> None:
    """Воркер фоновой перепроверки: проба эндпоинтов строки и публикация
    результата; в finally — очистка снимка и принудительное сохранение."""
    global _REPROBE
    try:
        reprobe_model(client_model)
    finally:
        with _REPROBE_LOCK:
            if _REPROBE is not None and client_model in _REPROBE:
                _REPROBE = None


def reprobe_model(client_model: str) -> bool:
    """Синхронная перепроба эндпоинтов строки модели (ядро, без сети — из
    воркера). Обновляет только endpoints/errors строки (и backend, если резолв
    сменился); calls/токены НЕ трогает. Возвращает True при успехе; False —
    строки нет в таблице. Исключения ловятся (лог), состояние очищается."""
    global _DIRTY
    with _TABLE_LOCK:
        _ensure_loaded_locked()
        row = _TABLE.get(client_model)
        if row is None:
            return False
        probing = row.get("probing")
    if probing:
        return False
    result = None
    backend_name = ""
    try:
        backend_cfg, resolved = config._resolve_backend(client_model)
        backend_name = backend_cfg["name"]
        if _backend_has_model(backend_cfg, resolved):
            result = _probe_model_endpoints(backend_cfg, resolved)
            _log_probe(client_model, backend_name, result)
            if result is not None:
                _sync_found_endpoints(backend_name, result)
        else:
            # Модели нет среди моделей бэкенда — пробовать нечем: колонки
            # эндпоинтов остаются «—».
            _log(
                client_model,
                f"reprobe skipped: model '{resolved}' not among backend '{backend_name}' models",
            )
    except Exception as e:  # noqa: BLE001 — перепроверка не должна валить
        _log(client_model, f"reprobe failed: {e}")
    finally:
        with _TABLE_LOCK:
            cur = _TABLE.get(client_model)
            if cur is not None:
                cur["backend"] = backend_name
                if result is not None:
                    cur["endpoints"] = result["endpoints"]
                    cur["errors"] = result["errors"]
                _DIRTY = True
    # Сохранить сразу (сид/воркер): результат пробы виден и после kill.
    _save_table(force=True)
    return True


# ==================== ТАРИФЫ МОДЕЛЕЙ (колонка Cost) ====================


def _as_float(v: object) -> float | None:
    """Число из YAML-значения тарифа; None — значение не число.

    Строки с запятой как десятичным разделителем ("0,02") нормализуются
    (запятая → точка; PyYAML такие строки числами не парсит). bool/int/float
    принимаются; bool трактуется как не-число (True→1.0 нежелателен)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", ".")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _normalize_tariff(raw: object) -> dict | None:
    """Привести запись тарифа из YAML к внутренней схеме; None — битая запись.

    Схема: {"name": str, "backend": str ("" — wildcard: любой бэкенд),
    "input_price": float, "output_price": float, "currency": str,
    "price_per": float (>= 1)}. Цены: отсутствующее поле/None → 0.0
    (бесплатная модель — не ошибка, Cost покажет «--»); отрицательные → 0;
    не-число → запись пропускается. price_per: 0/None/не-число → 1.0
    (защита деления на ноль). currency обязательна непустой строкой —
    без валюты в колонке Cost показывать нечего. name обязателен; backend
    опционален (отсутствие → wildcard)."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    backend = raw.get("backend")
    if backend is None:
        backend = ""
    elif not isinstance(backend, str):
        return None
    currency = raw.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        return None
    in_price = _as_float(raw.get("input_price"))
    out_price = _as_float(raw.get("output_price"))
    if in_price is None or out_price is None:
        return None  # цена-мусор — битая запись
    price_per = _as_float(raw.get("price_per"))
    if price_per is None or price_per <= 0:
        price_per = 1.0
    return {
        "name": name,
        "backend": backend,
        "input_price": max(in_price, 0.0),
        "output_price": max(out_price, 0.0),
        "currency": currency.strip(),
        "price_per": price_per,
    }


def _load_tariffs_locked() -> None:
    """(Пере)читать тарифы моделей с диска. Требует захваченного _TARIFF_LOCK.

    Источник — YAML-файл по config.ADAPTER_MODELS_TARIFFS (живое чтение —
    его могут менять тесты/пользователь между вызовами). Структура файла:
    {"tariffs": [записи]} (см. формат в config.py). Читает файл при КАЖДОМ
    вызове — файл маленький, а перечитывание требуется по событиям «загрузка
    usage-файла» (см. _ensure_loaded_locked) и «добавление новой модели»
    (см. record_model_usage): стоимость всегда считается по тарифу на момент
    отображения. Никогда не бросает наружу: пустой путь/битый/отсутствующий
    файл → пустые тарифы (колонка Cost — «--»), ошибка — в консольный лог.
    Заменяет _TARIFFS целиком (публикация новой ссылки под локом)."""
    global _TARIFFS
    path = (config.ADAPTER_MODELS_TARIFFS or "").strip()
    tariffs: dict[tuple[str, str], dict] = {}
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            raw_list = data.get("tariffs") if isinstance(data, dict) else None
            if isinstance(raw_list, list):
                for raw in raw_list:
                    t = _normalize_tariff(raw)
                    if t is not None:
                        # Ключ — (name, backend); последняя запись с тем же
                        # ключом перезаписывает предыдущую (порядок файла).
                        tariffs[(t["name"], t["backend"])] = t
        except Exception as e:  # noqa: BLE001 — тарифы не должны ронять учёт
            _log("tariffs", f"ignoring unreadable file {path}: {e}")
    _TARIFFS = tariffs


def ensure_tariffs_loaded() -> None:
    """(Пере)читать тарифы с диска (публичная обёртка для рендера).

    Вызывается webui_status._usage_rows_html перед рендером строк Models in
    use — каждый полноценный рендер страницы (GET "/") показывает Cost по
    тарифу на момент отображения; лёгкий поллинг usage_poll
    (/api/model-usage/snapshot) тарифы НЕ перечитывает (счётчики обновляет,
    Cost — производная, обновится при следующем рендере). Не бросает."""
    with _TARIFF_LOCK:
        _load_tariffs_locked()


def lookup_tariff(model: str, backend: str) -> dict | None:
    """Тариф модели для колонки Cost; None — модели нет в тарифах.

    Без сети и диска (читает загруженный кэш). Порядок матчинга: точная пара
    (модель, бэкенд) → тариф-«wildcard» (модель без backend — любой бэкенд)
    → None. Возвращает копию нормализованного тарифа — мутация результата не
    затрагивает кэш."""
    with _TARIFF_LOCK:
        t = _TARIFFS.get((model, backend))
        if t is None:
            t = _TARIFFS.get((model, ""))
        return dict(t) if t is not None else None


# ==================== ПЕРСИСТЕНТНОСТЬ (YAML) ====================


def _default_root() -> str:
    """Корень WEBUI — та же формула, что у webui_root в backend-adapter.py
    (ADAPTER_DEBUG_LOGPATH, всегда непуст; дефолт ./tmp/logs). Читается
    лениво (живое чтение config.ADAPTER_DEBUG_LOGPATH — его могут менять
    тесты между вызовами)."""
    return config.ADAPTER_DEBUG_LOGPATH


def _default_root_file() -> str:
    return os.path.join(_default_root(), MODEL_USAGE_FILE)


def _effective_persist_file() -> str:
    if _PERSIST_PATH:
        return _PERSIST_PATH
    return _default_root_file()


def _ensure_loaded_locked() -> None:
    """Ленивая загрузка таблицы из YAML-файла. Вызывается ВНУТРИ _TABLE_LOCK
    в начале record_model_usage / add_usage_tokens / usage_snapshot /
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
        # Синхронизация с _ENDPOINT_STATE: found-эндпоинты загруженных строк
        # (для бэкендов из числа настроенных) переносятся в кэш бэкендов —
        # колонка «Доступные API» и экспортёр после рестарта сразу видят
        # прошлые находки. Сети нет — перенос уже сохранённых результатов
        # (см. _sync_found_endpoints; загруженные строки не перепроверяются).
        for row in _TABLE.values():
            _sync_found_endpoints(row["backend"], row)
    except Exception as e:  # noqa: BLE001 — загрузка не должна ронять учёт
        _log("persist", f"ignoring unreadable file: {e}")
    finally:
        _LOADED = True
        # Загрузка накопленных счётчиков — точка перечитывания тарифов (см.
        # контракт в config.ADAPTER_MODELS_TARIFFS): стоимость на странице
        # после рестарта считается по текущему файлу тарифов.
        with _TARIFF_LOCK:
            _load_tariffs_locked()


def _read_persist_file(path: str) -> dict:
    """Прочитать YAML и вернуть {имя модели: сырая строка}. Поддерживает
    текущий формат ({"version": 2, "models": {...}}), формат версии 1
    ({"version": 1, "models": {...}} — байтовые строки; мигрирует: читатель
    возвращает строки как есть, _normalize_row отбрасывает bytes_*-поля,
    токены стартуют с 0) и плоский легаси (строки прямо в корне, без
    version/models — загружается как v1). Незнакомая версия (> 2) — файл
    игнорируется. Ошибки не бросает наружу: не-dict/битый YAML → пусто
    (обрабатывается в _ensure_loaded_locked)."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return {}
    version = data.get("version")
    if version is not None:
        try:
            version = int(version)
        except (TypeError, ValueError):
            return {}
        if version != 1 and version != _FORMAT_VERSION:
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

    Нормализация: имя — str(raw["model"] or model); счётчики и токены —
    неотрицательные int (иначе 0); endpoints — только dict, per-path
    status int|None и found bool (по status == 200, если нет), незнакомые
    pname отбрасываются (рендер ходит по config.ENDPOINT_PROBES с .get());
    errors — dict[str, str]; first_seen — строка HH:MM:SS (иначе текущее
    время); probing — всегда False (файл хранит только завершённые строки);
    неизвестные ключи (в т.ч. байтовые bytes_sent/bytes_recv из файлов
    версии 1) отбрасываются — токены мигрировавшей строки стартуют с 0."""
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
        "input_tokens": _to_int(raw.get("input_tokens")),
        "output_tokens": _to_int(raw.get("output_tokens")),
        "endpoints": endpoints,
        "errors": errors,
        "first_seen": first_seen,
        "probing": False,
    }


def _serialize_table() -> dict:
    """Снимок таблицы для записи: {"version": 2, "models": {...}}.
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
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "endpoints": {p: dict(ep) for p, ep in r["endpoints"].items()},
                "errors": dict(r["errors"]),
                "first_seen": r["first_seen"],
                "probing": False,
            }
    return {"version": _FORMAT_VERSION, "models": models}


def _atomic_write_yaml(path: str, payload: dict) -> None:
    """Атомарная запись YAML: временный файл в той же директории + os.replace.
    os.makedirs создаёт корень при необходимости. OSError НЕ ловится здесь —
    его обрабатывает _save_table (учёт не должен ронять запрос).

    Осиротевший временный файл прошлой прерванной записи (kill/Ctrl-C
    посреди safe_dump) затирается: open(..., "w") обрезает его содержимое,
    os.replace атомарно подменяет основной файл."""
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
    атомарно (tmp + os.replace); OSError — лог, учёт не роняет.

    _DIRTY снимается ТОЛЬКО после успешного os.replace (самый конец
    функции): прерванная запись (исключение/KeyboardInterrupt) оставляет
    флаг взведённым, и следующее сохранение допишет хвост. OSError ловится,
    KeyboardInterrupt — нет (его гасит flush_table/хэндлер завершения)."""
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


def _sync_found_endpoints(backend_name: str, source: dict) -> None:
    """Перенести found-эндпоинты источника в config._ENDPOINT_STATE бэкенда.

    Синхронизация с фоновой проверкой бэкендов: эндпоинт, найденный дымовой
    пробой МОДЕЛИ (found=True ⇔ HTTP 200), должен быть виден колонке
    «Доступные API» бэкенда на статус-странице и экспортёру, даже если
    фоновая проверка бэкенда его не пробовала. Сети здесь нет — перенос уже
    добытого результата (контракт «загруженные строки не перепроверяются»
    не нарушается).

    ``source`` — строка таблицы или результат _probe_model_endpoints с ключом
    ``endpoints``: {pname: {"status": int|None, "found": bool}} — по коротким
    именам ENDPOINT_PROBES (config.upsert_endpoint_state переводит их в пути).

    Переносятся ТОЛЬКО found-эндпоинты и ТОЛЬКО для бэкендов, присутствующих
    в config._BACKEND_BY_NAME: эндпоинты осиротевших бэкендов (удалены из
    YAML — строки usage-таблицы остаются как история) в кэш не добавляются.
    """
    if backend_name not in config._BACKEND_BY_NAME:
        return
    for pname, ep in source["endpoints"].items():
        if ep.get("found"):
            config.upsert_endpoint_state(backend_name, pname, ep.get("status"), True)


def _probe_model_endpoints(backend_cfg: dict, resolved: str) -> dict:
    """Синхронная проба эндпоинтов модели у бэкенда (вызывается вне лока).

    Политика пробы — «только явно указанные» (единая с фоновой проверкой
    config.probe_endpoints): эндпоинт пробуется ТОЛЬКО если он перечислен в
    ключе probe YAML-записи бэкенда с НЕПУСТОЙ моделью. Неперечисленные,
    пустые значения и отсутствие ключа probe вовсе — не пробуются. Пробуем
    resolved (моделью ЗАПРОСА), но только для эндпоинтов из probe-перечня
    бэкенда (политика «какие эндпоинты пробовать» едина с фоновой проверкой;
    «кем» в usage-строке — фактическая модель запроса). Если проба не задана
    ни для одного эндпоинта — возвращается пустой результат (модель учтена,
    проб нет; колонки эндпоинтов «—»).

    Зовёт низкоуровневую config._probe_backend_endpoints (та же
    классификация: found ⇔ HTTP 200, сеть/таймаут — found=False + текст в
    errors). Возвращает нормализованный к схеме записи результат:
      {"endpoints": {pname: {"status": int|None, "found": bool}}, "errors": {...}}
    — ключи по коротким именам ENDPOINT_PROBES (не пути), чтобы webui рендерил
    колонки без нормализации. Отдельная функция: её мокают тесты (в т.ч.
    _setup_adapter в test_server), не трогая config."""
    probe_cfg = backend_cfg.get("probe") or {}
    probes = {
        path: resolved for pname, path, _tpl in config.ENDPOINT_PROBES if probe_cfg.get(pname)
    }
    if not probes:
        return {"endpoints": {}, "errors": {}}
    result = config._probe_backend_endpoints(backend_cfg, probes, timeout=MODEL_USAGE_PROBE_TIMEOUT)
    endpoints = {}
    for pname, path, _tpl in config.ENDPOINT_PROBES:
        if path in result["endpoints"]:
            endpoints[pname] = result["endpoints"][path]
    return {"endpoints": endpoints, "errors": dict(result["errors"])}


def _log(client_model: str, msg: str) -> None:
    """Консольный лог строки [MODEL_USAGE]. Печатается БЕЗУСЛОВНО (консольные
    debug-логи не гейтятся; см. v0.8.6). config/model_usage не импортируют
    logger — корень DAG."""
    print(f"[MODEL_USAGE] model={client_model!r}: {msg}")


def _log_probe(client_model: str, backend_name: str, result: dict) -> None:
    """Лог-блок [MODEL_USAGE] первой пробы модели: сырые HTTP-коды по
    ENDPOINT_PROBES (единый формат с config._log_probe). Инкременты повторных
    обращений не логируются (спам при каждом запросе — как кэш-хиты пробы)."""
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
    "add_usage_tokens",
    "usage_snapshot",
    "reset_model_usage",
    "reset_model",
    "start_reprobe",
    "reprobe_state",
    "reprobe_model",
    "lookup_tariff",
    "ensure_tariffs_loaded",
]
