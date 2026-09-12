"""Configuration: env var reads, model mapping, multi-backend globals,
backend probe, YAML parser, SSL context, utility functions.

This is the heaviest module (root of the dependency DAG).
"""

import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

from . import probe_json  # JSON-дампы результатов проверок в LOGPATH (лист DAG)

# ==================== НАСТРОЙКИ ====================
PROXY_PORT = int(os.environ.get("ADAPTER_PROXY_PORT", "9999"))
# Адрес (host), на котором слушает HTTP-эндпоинт адаптера. Пусто / не задано —
# дефолт 127.0.0.1 (localhost). "0.0.0.0" — слушать на всех интерфейсах
# (доступ из сети). Идиом `or "127.0.0.1"`, а не get(name, default): пустая
# env-переменная в bind-кортеже socketserver означала бы INADDR_ANY (все
# интерфейсы) — с `or` и незаданная, и пустая дают безопасный localhost.
ADAPTER_ENDPOINT_HOST = os.environ.get("ADAPTER_ENDPOINT_HOST", "") or "127.0.0.1"
# Мастер-выключатель ФАЙЛОВОЙ записи debug/trace-логов и *.parts дампов:
#   ADAPTER_DEBUG_ENABLE=1 — файлы пишутся в ADAPTER_DEBUG_LOGPATH;
#   0 (по умолчанию) — на диск ничего не пишется (консольные debug-блоки
#   при этом БЕЗУСЛОВНЫ — печатаются всегда, независимо от этого флага).
ADAPTER_DEBUG = os.environ.get("ADAPTER_DEBUG_ENABLE", "0").lower() not in ("0", "false", "no", "")
# Единый путь к ДИРЕКТОРИИ логов сессий (debug-логи, trace, *.parts дампы)
# и корень веб-интерфейса (WEBUI, model-usage.yaml). ВСЕГДА непуст: пусто /
# не задано → дефолт "./tmp/logs" (относительно папки запуска). Папка
# создаётся на старте адаптера как корень WEBUI; лог-ФАЙЛЫ в неё пишутся
# только при ADAPTER_DEBUG_ENABLE=1 (см. ADAPTER_DEBUG выше). Режим «один
# файл» удалён — путь всегда директория.
ADAPTER_DEBUG_LOGPATH = os.environ.get("ADAPTER_DEBUG_LOGPATH", "") or "./tmp/logs"
ADAPTER_DETACH = os.environ.get("ADAPTER_DETACH_ENABLE", "0").lower() in ("1", "true", "yes")
ADAPTER_TIMEOUT = int(os.environ.get("ADAPTER_TIMEOUT", "300"))
ADAPTER_RETRY = int(os.environ.get("ADAPTER_RETRY_COUNT", "3"))
# Лимит консольного debug-вывода (v0.8.6-реформа): консоль — единственный
# ВСЕГДА-включённый канал, поэтому любая строка обрезается до N символов
# (0 = без обрезки). Файловый канал (session-*.log при ADAPTER_DEBUG_ENABLE=1)
# лимит НЕ уважает — туда пишутся полные части (см. trim_limit() и logger.py).
ADAPTER_DEBUG_TRIM = int(os.environ.get("ADAPTER_DEBUG_TRIM", "3000"))
ADAPTER_TRACE_REASONING_MAX_CHARS = int(os.environ.get("ADAPTER_TRACE_REASONING_MAX_CHARS", "0"))
ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS = int(os.environ.get("ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS", "0"))


ADAPTER_STRICT_MODELS = os.environ.get("ADAPTER_STRICT_MODELS", "1").lower() in ("1", "true", "yes")


def trim_limit() -> int:
    """Живой лимит обрезки консольного debug-вывода (0 = без обрезки).

    Читает модульный глобал ADAPTER_DEBUG_TRIM на каждый вызов — тот входит
    в RUNTIME_CONFIG_POOL и может быть изменён через /config без перезапуска
    (см. комментарий над RUNTIME_CONFIG_POOL: live-доступ `config.X`, не
    `from .config import X` — иначе снимок на импорте).
    """
    return ADAPTER_DEBUG_TRIM


# ADAPTER_DEBUG_PARTS — логический флаг: включить per-session дампы частей
# протокола (.json и .yaml парой) для ВСЕХ логгируемых частей (BODY,
# OPENAI_BODY, FETCH_RAW, TOOL_RESULT, RESPONSE — без фиксированного списка:
# каждая пишущая точка сама решает, какой тег дампить). Срабатывает только
# при ADAPTER_DEBUG_ENABLE=1, когда ADAPTER_DEBUG_LOGPATH задаёт директорию
# (файлы кладутся в неё). Пусто / 0 / false — выкл.
ADAPTER_DEBUG_PARTS = os.environ.get("ADAPTER_DEBUG_PARTS", "").lower() not in (
    "0",
    "false",
    "no",
    "",
)

# ==================== RUNTIME-ПЕРЕКЛЮЧАЕМЫЙ ПУЛ (см. /config эндпойнт) ====================
# Подмножество переменных выше, которые можно менять НЕ ПЕРЕЗАПУСКАЯ адаптер —
# через HTTP API /config (webui_config_api.py), эндпойнт общего WEBUI-сервера
# (WEBUI поднимается всегда — см. backend-adapter.py).
# Идея: включать накопление логов/трейсов/*.parts-дампов на время диагностики
# конкретной проблемы и выключать обратно, без остановки самого прокси.
#
# Пул управляет только тем, что БЕЗОПАСНО менять на лету: объём записи на
# диск (логи/трейсы/дампы) и поведение НОВЫХ запросов (рубильники стриминга,
# строгая валидация моделей, санитайзер логов). Принцип отбора — значение
# применяется на следующем же вызове/запросе и не рвёт активные соединения.
# Сеть/бэкенды/порты/адреса/точка хранения сюда не входят — их
# runtime-переключение требует пересоздания слушателей, повторной
# инициализации бэкендов или смены раскладки файлов посреди сессии
# (подробный разбор — в docs/environment.md, «Runtime-пул»).
#
# ВАЖНО для того, кто ЧИТАЕТ эти переменные в других модулях: все места
# использования (server.py, streaming.py, convert.py, tracer.py, logger.py)
# переведены на "живое" чтение через `config.ADAPTER_X`, а НЕ через
# `from .config import ADAPTER_X` на уровне модуля — второе сделало бы
# разовый снимок при импорте, и set_runtime_config() ниже не имел бы эффекта
# нигде, кроме этого файла. Если добавляете сюда новую переменную — проверьте
# ВСЕ её точки чтения на этот же паттерн. Исключение — ADAPTER_MODELS_MAPPING:
# его читает не сам server.py, а словарь _MAP, который server.py импортирует
# ПО ССЫЛКЕ; поэтому set_runtime_config мутирует _MAP на месте (см. ниже),
# а не переприсваивает словарь.
#
# ПРИМЕЧАНИЕ: ADAPTER_DEBUG_LOGPATH сюда сознательно НЕ входит — это точка
# хранения (корень WEBUI и лог-директория), а не «переключатель объёма»:
# смена пути на лету потребовала бы пересоздания корня веб-сервера и
# раскладки файлов посреди сессии — за рамками задачи. (Файловая запись
# переключается пулом через ADAPTER_DEBUG: заданный LOGPATH всегда непуст,
# поэтому 1 через /config сразу начнёт писать в него.)
RUNTIME_CONFIG_POOL = (
    "ADAPTER_DEBUG",
    "ADAPTER_DEBUG_PARTS",
    "ADAPTER_DEBUG_TRIM",
    "ADAPTER_SENSITIVE_LOGGING_ENABLE",
    "ADAPTER_STREAMING_ENABLE",
    "ADAPTER_STREAM_INCLUDE_USAGE",
    "ADAPTER_STRICT_MODELS",
    "ADAPTER_TRACE_REASONING_MAX_CHARS",
    "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS",
    # TARGET-маршрутизация входов (v0.9.1): целевой формат/режим каждого из
    # трёх входных эндпоинтов. Значение применяется на следующем же запросе
    # (routing.target_for_input читает config.ADAPTER_*_TARGET на каждый
    # вызов) и влияет только на НОВЫЕ запросы, не рвя активные соединения —
    # потому входит в пул, хотя меняет «топологию восприятия» входов (см.
    # блок «РОУТИНГ ВХОДНЫХ ЭНДПОИНТОВ» ниже: там же объявления из env).
    "ADAPTER_MESSAGES_TARGET",
    "ADAPTER_COMPLETIONS_TARGET",
    "ADAPTER_RESPONSES_TARGET",
    # Маппинг моделей agent→backend (v0.9.3): строка формата
    # ``agent:backend,agent2:backend2``. Меняется на лету через /config —
    # set_runtime_config мутирует config._MAP НА МЕСТЕ (server.py держит
    # ссылку на словарь через `from .config import _MAP`), поэтому следующий
    # же запрос резолвит модель по новому маппингу без перезапуска.
    "ADAPTER_MODELS_MAPPING",
)

# Допустимые значения TARGET-переменных (конкретный формат-цель
# messages|completions|responses → прямое преобразование, passthrough →
# дословная передача без преобразования, none → вход выключен).
# Единый источник: объявления выше и _parse_target читают этот кортеж,
# routing.decide валидирует значение assert-ом по нему (routing.py),
# WEBUI /config рендерит выпадающий список из него.
TARGET_ALLOWED_VALUES = ("messages", "completions", "responses", "passthrough", "none")

# Типы для валидации входа /config (POST): bool, int или enum-домен — кортеж
# (str, допустимые_значения) для строковых полей с фиксированным набором
# значений (в пуле это три TARGET-переменные). Остальное отклоняем.
_RUNTIME_CONFIG_TYPES = {
    "ADAPTER_DEBUG": bool,
    "ADAPTER_DEBUG_PARTS": bool,
    "ADAPTER_DEBUG_TRIM": int,
    "ADAPTER_SENSITIVE_LOGGING_ENABLE": bool,
    "ADAPTER_STREAMING_ENABLE": bool,
    "ADAPTER_STREAM_INCLUDE_USAGE": bool,
    "ADAPTER_STRICT_MODELS": bool,
    "ADAPTER_TRACE_REASONING_MAX_CHARS": int,
    "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS": int,
    "ADAPTER_MESSAGES_TARGET": ("enum", TARGET_ALLOWED_VALUES),
    "ADAPTER_COMPLETIONS_TARGET": ("enum", TARGET_ALLOWED_VALUES),
    "ADAPTER_RESPONSES_TARGET": ("enum", TARGET_ALLOWED_VALUES),
    # Строка маппинга моделей — свободный текст (домен не фиксирован;
    # синтаксис разбирает _parse_models_mapping, лояльный как у env).
    "ADAPTER_MODELS_MAPPING": str,
}


def get_runtime_config() -> dict:
    """Текущие значения runtime-переключаемого пула — для GET /config."""
    return {name: globals()[name] for name in RUNTIME_CONFIG_POOL}


def set_runtime_config(**kwargs) -> dict:
    """Меняет подмножество RUNTIME_CONFIG_POOL — для POST /config.

    Тот же приём, что уже применяется в этом файле для _AVAILABLE_MODELS/
    _MODEL_TO_BACKEND (см. refresh_models() ниже) — переприсваивание
    модульных глобалов через `global`. Для _AVAILABLE_MODELS/_MODEL_TO_BACKEND
    там используется МУТАЦИЯ НА МЕСТЕ (.clear()+.update()), т.к. это словари
    и их импортируют по ссылке в других модулях; здесь же пул — bool/int/
    str-скаляры, которые в Python в принципе нельзя мутировать на месте,
    поэтому единственный рабочий вариант — переприсваивание через `global`
    ЗДЕСЬ, в сочетании с тем, что все читатели переведены на live-доступ
    `config.X` (см. комментарий над RUNTIME_CONFIG_POOL).

    Неизвестные ключи и ключи вне пула ИГНОРИРУЮТСЯ МОЛЧА (не 400 — иначе
    один опечатанный лишний ключ в теле запроса откатил бы все остальные
    валидные изменения; вызывающий (webui_config_api.py) сверяет ответ с тем,
    что послал, и сам решает, как об этом сообщить). Значение неверного типа
    для известного ключа — тоже игнорируется (не применяется), остальные
    ключи всё равно применяются. Возвращает get_runtime_config() ПОСЛЕ
    применения — вызывающий видит, что реально изменилось.
    """
    global ADAPTER_DEBUG, ADAPTER_DEBUG_PARTS, ADAPTER_DEBUG_TRIM
    global ADAPTER_SENSITIVE_LOGGING_ENABLE, ADAPTER_STREAMING_ENABLE
    global ADAPTER_STREAM_INCLUDE_USAGE, ADAPTER_STRICT_MODELS
    global ADAPTER_TRACE_REASONING_MAX_CHARS, ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS
    global ADAPTER_MESSAGES_TARGET, ADAPTER_COMPLETIONS_TARGET, ADAPTER_RESPONSES_TARGET
    global ADAPTER_MODELS_MAPPING

    for name, value in kwargs.items():
        if name not in RUNTIME_CONFIG_POOL:
            continue
        expected = _RUNTIME_CONFIG_TYPES[name]
        # bool — подкласс int в Python: проверяем bool ДО int, иначе
        # int-поле молча приняло бы True/False как 1/0.
        if expected is bool and not isinstance(value, bool):
            continue
        if expected is int and (isinstance(value, bool) or not isinstance(value, int)):
            continue
        # str-поле (ADAPTER_MODELS_MAPPING): значение — строка как есть
        # (синтаксис разбирает _parse_models_mapping, лояльный как у env).
        if expected is str and not isinstance(value, str):
            continue
        # enum-поле: значение — строка из допустимого набора (TARGET).
        # Невалидная строка (или не-строка) игнорируется, как неверный тип.
        if (
            isinstance(expected, tuple)
            and expected[0] == "enum"
            and (not isinstance(value, str) or value not in expected[1])
        ):
            continue
        globals()[name] = value
        # Маппинг моделей — особый случай: server.py держит ССЫЛКУ на словарь
        # _MAP (`from .config import _MAP`), поэтому переприсваивание
        # ADAPTER_MODELS_MAPPING в одиночку не обновило бы живой резолвер.
        # Мутируем _MAP НА МЕСТЕ (clear+update), как _AVAILABLE_MODELS/
        # _MODEL_TO_BACKEND в _init_multi_backends — новый маппинг виден
        # следующему же запросу.
        if name == "ADAPTER_MODELS_MAPPING":
            _MAP.clear()
            _MAP.update(_parse_models_mapping(value))

    return get_runtime_config()


# ===================================================

# Веб-интерфейс — общий статус адаптера + просмотр сессий. Поднимается
# ВСЕГДА (флага отключения нет): статус-страница "/" (версия, режим,
# LLM-эндпоинты, таблица «Models in use») + health-эндпоинты (/healthz,
# /live, /ready) + /session (просмотр *.parts сессий; при ADAPTER_DEBUG_ENABLE=0
# логов нет — вкладки сессий пусты) + /config (runtime-пул). Корень —
# директория ADAPTER_DEBUG_LOGPATH (см. выше; дефолт ./tmp/logs) — там же
# лежит model-usage.yaml. Порт — ADAPTER_WEBUI_PORT; адрес — ADAPTER_WEBUI_HOST
# (пусто/не задано → дефолт 127.0.0.1, только локально).
ADAPTER_WEBUI_PORT = int(os.environ.get("ADAPTER_WEBUI_PORT", "8765"))
# Адрес, на котором слушает веб-интерфейс; дефолт 127.0.0.1 (только
# локально). "0.0.0.0" — доступ из сети (внимание: содержимое сессий —
# git log, файлы, reasoning — не должно случайно утечь).
ADAPTER_WEBUI_HOST = os.environ.get("ADAPTER_WEBUI_HOST", "") or "127.0.0.1"
# Prometheus-экспортёр — отдельный лёгкий слушатель метрик (см.
# prometheus_exporter.py): включается вместе с WEBUI по умолчанию
# (ADAPTER_EXPORTER_ENABLE=1), адрес — тот же ADAPTER_WEBUI_HOST, порт —
# ADAPTER_EXPORTER_PORT. /metrics — text exposition 0.0.4 без библиотек.
ADAPTER_EXPORTER_ENABLE = os.environ.get("ADAPTER_EXPORTER_ENABLE", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
ADAPTER_EXPORTER_PORT = int(os.environ.get("ADAPTER_EXPORTER_PORT", "9100"))
# Отключение санитайзера: при 1 — _d(), _dr() и _trace() записывают строки
# без вызова redact(), логируются полные токены, заголовки, ключи.
# По умолчанию false — санитайзер активен, секреты маскируются.
ADAPTER_SENSITIVE_LOGGING_ENABLE = os.environ.get(
    "ADAPTER_SENSITIVE_LOGGING_ENABLE", "0"
).lower() in ("1", "true", "yes")
# Управляющий флаг для двух режимов работы адаптера:
#   1 (по умолчанию) — "потоковый" режим: если клиент (Claude Code) просит
#     stream=true, адаптер честно пробрасывает это бэкенду и стримит SSE
#     построчно (см. stream_openai_to_anthropic) — это и есть исправление
#     первопричины BrokenPipeError, разобранное выше.
#   0/false/no — "совместимый" режим: полный откат к старому поведению —
#     адаптер ВСЕГДА шлёт бэкенду stream=False и ждёт ответ целиком,
#     независимо от того, что просил клиент. Оставлено как аварийный
#     рубильник — например, если конкретный backend плохо/нестандартно
#     стримит SSE и надёжнее временно вернуться к нестриминговому пути,
#     не откатывая сам файл адаптера.
ADAPTER_STREAMING_ENABLE = os.environ.get("ADAPTER_STREAMING_ENABLE", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
# БАГ 2026-08-27: в потоковом режиме адаптер НЕ просил backend прислать
# usage в SSE (OpenAI-совместимый стриминг отдаёт usage только при явном
# stream_options.include_usage=true) — из-за этого клиент (Claude Code)
# всю сессию видел input_tokens=0 и не мог корректно оценивать заполнение
# контекстного окна (см. stream_openai_to_anthropic и message_delta ниже).
# Флаг-рубильник на случай backend'а, который не понимает stream_options
# и падает на неизвестном поле (такое встречается у части OpenAI-совместимых
# серверов старых версий) — тогда можно откатиться, не трогая сам файл.
ADAPTER_STREAM_INCLUDE_USAGE = os.environ.get("ADAPTER_STREAM_INCLUDE_USAGE", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
# ===================================================

# ==================== МАППИНГ МОДЕЛЕЙ (agent -> backend) ====================
# Строка формата ``agent_model:backend_model,agent2:backend2``. Входит в
# RUNTIME_CONFIG_POOL (v0.9.3): правится на лету через /config — при
# применении set_runtime_config перестраивает словарь _MAP НА МЕСТЕ
# (см. ниже), поэтому server.py (`from .config import _MAP`) видит новый
# маппинг без перезапуска адаптера.
ADAPTER_MODELS_MAPPING = os.environ.get("ADAPTER_MODELS_MAPPING", "")
# ===================================================

# ==================== BACKEND CONFIG ====================
ADAPTER_BACKEND_CONFIG = os.environ.get("ADAPTER_BACKEND_CONFIG", "")
# Мастер-флаг «дымовой» пробы API-эндпойнтов бэкендов (config.refresh_models →
# probe_endpoints): при 1 каждый refresh моделей дополнительно пробует у каждого
# бэкенда известные эндпойнты API короткими запросами max_tokens:1 (результат —
# на статус-странице WEBUI «Доступные API» и в консоли как [ENDPOINT_PROBE]).
# 0 — автопроба отключена (колонка «не опрошено», сетевых POST-проб нет).
# Кэш результатов — ENDPOINT_PROBE_TTL секунд (повторные заходы на страницу
# в течение TTL не дублируют запросы к бэкенду).
ADAPTER_ENDPOINT_PROBE = os.environ.get("ADAPTER_ENDPOINT_PROBE", "1").lower() in (
    "1",
    "true",
    "yes",
)

# Таблица использованных моделей (см. backend_adapter/model_usage.py): учёт
# клиентских моделей, прошедших strict-проверку (имя из BODY, до маппинга).
# При ПЕРВОМ обращении к новой модели — синхронная дымовая проба 4 известных
# эндпоинтов ИМЕННО этой моделью (первый запрос модели ждёт до 4×10 с);
# повторные обращения не перепроверяют никогда. 1 — проба выполняется при
# первом обращении ВСЕГДА, независимо от ADAPTER_ENDPOINT_PROBE (тот управляет
# только фоновой пробой бэкендов refresh_models). 0 — пробы отключены, учёт
# обращений остаётся (колонки эндпоинтов — «—»). Таблица персистентна:
# сохраняется в YAML-файл model-usage.yaml в корне WEBUI (см.
# ADAPTER_MODEL_USAGE_SAVE_INTERVAL ниже) и загружается при старте;
# в RUNTIME_CONFIG_POOL не входит.
ADAPTER_MODEL_USAGE_ENABLE = os.environ.get("ADAPTER_MODEL_USAGE_ENABLE", "1").lower() in (
    "1",
    "true",
    "yes",
)

# ==================== РОУТИНГ ВХОДНЫХ ЭНДПОИНТОВ (TARGET) ====================
# Три env-переменные — по одной на каждый входной POST-эндпоинт адаптера
# (префикс имени переменной = входной эндпоинт): ADAPTER_MESSAGES_TARGET
# управляет приёмом на /v1/messages, ADAPTER_COMPLETIONS_TARGET — на
# /v1/chat/completions, ADAPTER_RESPONSES_TARGET — на /v1/responses.
# Значение задаёт, ЧТО делать с запросом на этом входе:
#   messages|completions|responses — конкретный формат-цель: ПРЯМОЕ
#       преобразование запроса входа в указанный формат (реестр
#       routing.IMPLEMENTED_CONVERSIONS; сейчас messages→completions и
#       messages→messages — сортировка system в начало). Нереализованная
#       пара даёт ошибку агенту (400 «conversion … is not implemented»);
#   passthrough — передать на соответствующий входу эндпойнт бэкенда БЕЗ
#       преобразования: тело и SSE уходят дословно (E→E);
#   none — входной эндпоинт выключен (404) — безопасный дефолт.
# Zero-config дефолты описывают текущие возможности конвертера:
# MESSAGES=completions (принимается только /v1/messages, конвертация в chat
# completions), COMPLETIONS=none, RESPONSES=none.
# Входят в RUNTIME_CONFIG_POOL (см. выше): значение меняется на лету через
# /config (routing.target_for_input читает config.ADAPTER_*_TARGET на каждый
# запрос) и влияет только на НОВЫЕ запросы, не рвя активные соединения.
# Допустимый набор значений — единая константа TARGET_ALLOWED_VALUES в блоке
# пула (тип ("enum", …) в _RUNTIME_CONFIG_TYPES). Невалидное/пустое значение
# env при импорте НЕ роняет старт: консольный [WARN] + трактовка как 'none'
# (безопасное выключение входа). Это же покрывает значение 'auto' прежних
# версий (удалено в v0.9.4): [WARN] + 'none'.


def _parse_target(value: str, var_name: str) -> str:
    """Нормализация значения TARGET-переменной (нижний регистр, strip).

    Невалидное/пустое значение не роняет старт (это рубильник поведения,
    а не жёсткое требование как ADAPTER_BACKEND_CONFIG): печатается
    консольный [WARN], значение трактуется как 'none' — вход выключен
    (безопасный дефолт). Значение 'auto' прежних версий попадает сюда же."""
    raw = (value or "").strip().lower()
    if raw in TARGET_ALLOWED_VALUES:
        return raw
    if value and value.strip():
        print(
            f"[WARN] {var_name}: invalid value {value!r} (expected "
            f"messages|completions|responses|passthrough|none) — treating as 'none'"
        )
    return "none"


# Объявления из env (импорт): значения попадают в модульные глобалы, которые
# входят в RUNTIME_CONFIG_POOL и могут быть переприсвоены на лету через /config
# (set_runtime_config). Дефолты — zero-config поведение (см. комментарий выше).
ADAPTER_MESSAGES_TARGET = _parse_target(
    os.environ.get("ADAPTER_MESSAGES_TARGET", "completions"), "ADAPTER_MESSAGES_TARGET"
)
ADAPTER_COMPLETIONS_TARGET = _parse_target(
    os.environ.get("ADAPTER_COMPLETIONS_TARGET", "none"), "ADAPTER_COMPLETIONS_TARGET"
)
ADAPTER_RESPONSES_TARGET = _parse_target(
    os.environ.get("ADAPTER_RESPONSES_TARGET", "none"), "ADAPTER_RESPONSES_TARGET"
)

# Период персистентного сохранения таблицы использованных моделей в YAML
# (сек). «Грязная» таблица сохраняется не чаще раза в
# ADAPTER_MODEL_USAGE_SAVE_INTERVAL; создание новой строки модели, сброс
# строки и завершение работы адаптера сохраняют сразу (flush_table).
# Дефолт 300 — приемлемая потеря хвоста ≤ 300 с (5 мин) при жёстком kill;
# при штатном завершении таблица сохраняется всегда.
ADAPTER_MODEL_USAGE_SAVE_INTERVAL = int(os.environ.get("ADAPTER_MODEL_USAGE_SAVE_INTERVAL", "300"))

# Глубина таблицы активных сессий агентов на статус-странице WEBUI (секция
# «Sessions», v0.9.2): сколько последних СТРОК держать в памяти. Строка —
# кортеж (session, agent, model, backend, route), поэтому одна сессия может
# занимать несколько строк (смена модели/обработчика); лимит считает строки,
# а не сессии. Таблица НЕ персистится — при каждом запуске адаптера пуста.
# Значение — живой лимит: читается session_registry._limit() при каждой
# регистрации обращения; 0 — таблица отключена (ничего не хранится). В
# runtime-пул (/config) не входит — смена требует перезапуска, как у прочих
# лимитов.
ADAPTER_SESSIONS_TABLE = int(os.environ.get("ADAPTER_SESSIONS_TABLE", "10"))

# ADAPTER_SESSION_HEADER — список имён HTTP-заголовков (через запятую), из
# которых адаптер берёт идентификатор сессии агента. Побеждает ПЕРВЫЙ непустой
# из списка; если ни одного — сессия протоколируется как «unknown». Имена
# сверяются без учёта регистра (штатное поведение email.message.Message).
# Зачем список: разные агенты называют заголовок по-разному. [CC] CLI шлёт
# X-Claude-Code-Session-Id; QwenCode id по умолчанию не шлёт вовсе и
# настраивается через customHeaders — имя выбирает пользователь, штатно
# указывают то же X-Claude-Code-Session-Id, второй кандидат (x-opencode-session)
# — для клиентов с отдельным именем (см. docs/environment.md, раздел
# «Настройка агента (QwenCode)»). Пустое значение переменной трактуется как
# дефолт. В runtime-пул (/config) не входит — смена требует перезапуска.
#
# Синтаксис элемента списка: «Имя-заголовка» — плоский заголовок (как раньше),
# либо «Имя-заголовка:ключ» — значение разбирается как JSON-объект и берётся
# строковое поле «ключ» верхнего уровня (Codex CLI шлёт id внутри
# x-codex-turn-metadata как {"session_id": "...", ...}). Битый JSON, отсутствие
# ключа или нестроковое значение — кандидат молча пропускается. Двоеточие в
# имени HTTP-заголовка невозможно, поэтому старые значения без «:» трактуются
# ровно как прежде. x-client-request-id — пример opt-in: Codex дублирует в нём
# тот же uuid, но имя generic (у части клиентов это per-request id), поэтому в
# дефолт не входит.
_DEFAULT_SESSION_HEADERS = (
    "X-Claude-Code-Session-Id,x-opencode-session,x-codex-turn-metadata:session_id"
)
ADAPTER_SESSION_HEADER = (
    os.environ.get("ADAPTER_SESSION_HEADER", "").strip() or _DEFAULT_SESSION_HEADERS
)

# ADAPTER_MODELS_TARIFFS — путь к YAML-файлу с тарифами моделей (для колонки
# Cost таблицы «Models in use» на статус-странице WEBUI). Формат (запятая —
# десятичный разделитель цен):
#   tariffs:
#     - name: model-name        # имя клиентской модели (как в BODY запроса)
#       backend: provider-name  # опционально; при задании тариф матчится
#                               #   только для этого бэкенда, без поля — для
#                               #   любого бэкенда (wildcard)
#       input_price: 10         # цена за price_per входных токенов
#       output_price: 20        # цена за price_per выходных токенов
#       currency: RUB           # 3-буквенный код валюты (USD, RUB, …)
#       price_per: 1000000      # база — на сколько токенов дана цена
#                               #   (дефолт 1; 0/None трактуется как 1)
# Пусто / не задано / файл не читается — колонка Cost показывает «--».
# Нулевые цены — бесплатная модель (не ошибка; Cost тоже «--»: токены есть,
# цена 0 → сумма 0). Список перечитывается с диска при загрузке накопленных
# счётчиков из model-usage.yaml и при добавлении новой модели в таблицу
# работающего адаптера (см. model_usage._load_tariffs_locked) — стоимость
# всегда считается по тарифу на момент отображения.
ADAPTER_MODELS_TARIFFS = os.environ.get("ADAPTER_MODELS_TARIFFS", "")

# Глобальные структуры конфигурации бэкендов (единственный режим — YAML).
# _BACKENDS — список [{name, base, key}, …]
# _BACKEND_BY_NAME — name → config
# _MODEL_TO_BACKEND — model_id → (backend_name, backend_config)
# _DEFAULT_BACKEND — первый бэкенд в списке (fallback)
_BACKENDS: list[dict] = []
_BACKEND_BY_NAME: dict[str, dict] = {}
_MODEL_TO_BACKEND: dict[str, tuple] = {}
_DEFAULT_BACKEND: dict | None = None
# ===================================================


def _parse_models_mapping(raw: str) -> dict[str, str]:
    """Разбирает строку вида ``a:b,c:d`` в {a: b, c: d}.
    Пустая строка → пустой словарь (маппинг отключён)."""
    if not raw or not raw.strip():
        return {}
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        agent_model, backend_model = pair.split(":", 1)
        agent_model = agent_model.strip()
        backend_model = backend_model.strip()
        if agent_model and backend_model:
            result[agent_model] = backend_model
    return result


_MAP: dict[str, str] = _parse_models_mapping(ADAPTER_MODELS_MAPPING)


# ==================== MULTI-BACKEND: YAML PARSER ====================
def _parse_backend_yaml(path: str) -> list[dict] | None:
    """Мини-парсер для YAML-файла бэкендов.

    Ожидаемая структура:
        backend:
          - name: AAA
            base: https://llm.service.example.com
            key: ADAPTER_BACKEND_KEY_AAA
            probe:                    # НЕобязательно — см. ниже
              - completions: qwen3.6
              - messages:
              - responses: gpt-5-sol
              - embeddings: text-embedding-3-small
          - name: BBB
            base: https://llm.service.another.com
            key: ADAPTER_BACKEND_KEY_BBB

    ``probe`` — необязательный ключ записи: какие API-эндпойнты бэкенда
    пробовать и какой моделью (см. probe_endpoints). Список пар
    ``<эндпойнт>: <модель>``. Политика «только явно указанные пробы»:
    пробуется ТОЛЬКО эндпойнт, перечисленный с НЕПУСТОЙ моделью;
    неперечисленные и пустые значения (``messages:``) НЕ пробуются.
    Ключа ``probe`` нет — не пробуется ничего. Порядок пар сохраняется.

    Возвращает список dict: {name, base, key} (+ probe, если задан) или
    None при ошибке."""
    try:
        with open(path) as f:
            raw = f.read()
    except Exception as e:
        print(f"[BACKEND_CONFIG] Failed to read {path}: {e}")
        return None

    # Удаляем YAML-документные маркеры
    lines = raw.splitlines()
    blocks = []
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        # Пропускаем пустые строки, комментарии, document markers
        if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
            continue
        # Пропускаем корневую ключевую строку "backend:"
        if stripped == "backend:" and current is None:
            continue
        # Новая запись в списке: "  - name: AAA"
        m = re.match(r"^\s*-\s+name:\s*(.+)$", line)
        if m:
            if current is not None:
                blocks.append(current)
            current = {"name": m.group(1).strip().strip('"').strip("'")}
            continue
        # Продолжение текущей записи: "    base: ...", "    key: ...",
        # либо строка подблока "probe:" ("      - completions: qwen3.6").
        if current is not None:
            m2 = re.match(r"^\s+(\w+):\s*(.*)$", line)
            if m2 and m2.group(1) in ("name", "base", "key"):
                current[m2.group(1)] = m2.group(2).strip().strip('"').strip("'")
                continue
            # Строка подблока probe: "      - <эндпойнт>: <модель>"
            # (значение может быть пустым — «не пробовать»; имя эндпойнта —
            # слово, возможно с дефисами).
            mp = re.match(r"^\s+-\s+([\w-]+):\s*(.*)$", line)
            if mp:
                current.setdefault("probe", {})[mp.group(1)] = (
                    mp.group(2).strip().strip('"').strip("'")
                )

    if current is not None:
        blocks.append(current)

    # Валидация + раскрытие переменных окружения в поле key
    valid_blocks: list[dict] = []
    for b in blocks:
        if not all(k in b for k in ("name", "base", "key")):
            print(f"[BACKEND_CONFIG] Skipping invalid entry: {b}")
            continue
        # key из YAML — имя переменной окружения (например "ADAPTER_HOME_KEY");
        # заменяем на реальное значение. Если переменная не задана — оставляем
        # строку как есть (будет ошибка при использовании, но конфиг валиден).
        env_var = b["key"]
        resolved = os.environ.get(env_var, env_var)
        b["key"] = resolved
        valid_blocks.append(b)

    return valid_blocks if valid_blocks else None


# ==================== MULTI-BACKEND: PROBE & INIT ====================
_AVAILABLE_MODELS: dict[str, dict] = {}


def _cap(text: str, max_chars: int) -> str:
    """Обрезает text до max_chars, если max_chars > 0. 0/не задано — без
    ограничения (используется по умолчанию для reasoning/tool-полей trace,
    в отличие от старого поведения, которое обрезало их безусловно)."""
    if not text or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...[TRUNCATED {len(text) - max_chars} chars]"


SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def _fetch_models(base: str, key: str, timeout: float | None = None) -> list[dict]:
    """Запрашивает GET /v1/models у бэкенда, возвращает список dict.

    ``base`` — URL бэкенда, ``key`` — уже раскрытый токен (в т.ч. из
    ADAPTER_BACKEND_CONFIG). Извлечение из os.environ — в _parse_backend_yaml.
    ``timeout`` — таймаут urlopen в секундах; None → ADAPTER_TIMEOUT
    (используется refresh-ом из веб-страницы с коротким таймаутом, чтобы
    страница статуса не висела по 300 с при недоступном бэкенде).
    """
    url = base.rstrip("/") + "/v1/models"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        },
        method="GET",
    )
    try:
        resp = urllib.request.urlopen(
            req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT if timeout is None else timeout
        )
    except Exception as e:
        print(f"[FETCH_ERROR] Failed to fetch models from {url}: {e}")
        raise
    raw = resp.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[FETCH_ERROR] json decode failed: {e} raw={raw[:500]}")
        raise
    if isinstance(data, list):
        return data
    return data.get("data", [])


def _write_models_snapshot(
    bname: str,
    bmodels: list[dict] | None,
    error: str | None = None,
) -> None:
    """JSON-дамп результата проверки бэкенда на доступные модели.

    Безусловный наблюдательный канал (v0.9.0): файл
    ``<имя_бэкенда>.models.json`` пишется в ADAPTER_DEBUG_LOGPATH при каждой
    проверке — стартовой (_init_multi_backends) и фоновой (refresh_models /
    reload-перечитывания) — каждый раз перезаписываясь целиком. Вне
    ADAPTER_DEBUG_ENABLE / ADAPTER_DEBUG_PARTS / TRIM (гейт — только наличие
    LOGPATH); снимок содержит ПОЛНЫЕ записи моделей из ответа /v1/models
    (redact-маскирование секретов — внутри probe_json). Провал записи молча
    глотается модулем probe_json — проверку не роняет."""
    payload = {
        "backend": bname,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ok": error is None and bmodels is not None,
    }
    if error is not None:
        payload["error"] = error
    if bmodels is not None:
        payload["count"] = len(bmodels)
        # Копии записей ответа — в файл уходит снимок на момент проверки
        # (бэкенд мог поменять список моделей к следующему чтению).
        payload["models"] = [dict(m) for m in bmodels]
    probe_json.write_models_json(bname, payload)


# ==================== MULTI-BACKEND: ENDPOINT PROBE ====================
# «Дымовая» проба API-эндпойнтов бэкенда: короткий POST (max_tokens:1) на
# каждый известный путь — определить, какие эндпойнты бэкенд реально
# обслуживает, не тратя токены на содержательный ответ. Выполняется при
# каждом refresh_models — а тот вызывается из фонового воркера проверки
# (start_refresh: при старте адаптера, на первом GET "/" и по кнопке 🔃
# «Перепроверить бэкенды»), не при каждой загрузке страницы; результат —
# колонка «Доступные API» на странице и лог-строка [ENDPOINT_PROBE] в
# консоли. Мастер-флаг — ADAPTER_ENDPOINT_PROBE (0 — автопроба отключена).
#
# Классификация по HTTP-коду ответа (см. tmp/plan-llm-endpoints.md):
#   200                  — эндпоинт работает (found=True);
#   прочие 4xx/5xx (вкл. 400/401/405/429) и 404 — эндпоинт НЕ работает
#                         (found=False): тело/ключ не подошли, либо путь
#                         не реализован — на странице такие не показываются;
#   сеть/таймаут         — ошибка бэкенда целиком (found=False + текст).
# Политика «только явно указанные пробы»: пробуется ТОЛЬКО эндпойнт,
# перечисленный в ``probe`` записи backend с НЕПУСТОЙ моделью (этой
# моделью). Неперечисленные эндпойнты, пустые значения (``messages:``)
# и бэкенды без ключа ``probe`` вовсе НЕ пробуются — это штатное
# состояние, а не ошибка (см. probe_endpoints). Модель из ``probe``, не
# найденная среди моделей бэкенда в /v1/models, пропускается точечно.
ENDPOINT_PROBES: tuple[tuple[str, str, dict], ...] = (
    ("completions", "/v1/chat/completions", {"max_tokens": 1}),
    ("messages", "/v1/messages", {"max_tokens": 1}),
    ("responses", "/v1/responses", {"max_output_tokens": 1}),
    ("embeddings", "/v1/embeddings", {}),
)
# TTL кэша результатов пробы: повторные заходы на статус-страницу в течение
# этого окна НЕ дублируют запросы к бэкенду (кэш-хит отдаётся без сети).
ENDPOINT_PROBE_TTL = 60.0

# Кэш результатов пробы по бэкендам — мутируется на месте (как
# _AVAILABLE_MODELS), потому что webui_status.py импортирует его по ссылке.
# Ключ — имя бэкенда; значение: {"at": float(ts), "endpoints": {<путь>:
# {"status": int|None, "found": bool}}, "errors": {...}}.
_ENDPOINT_STATE: dict[str, dict] = {}


def _http_json(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict,
    timeout: float | None,
) -> tuple[int | None, dict | None, str | None]:
    """Низкоуровневый JSON-запрос к бэкенду (для дымовой пробы эндпойнтов).

    Образец — ``_fetch_models`` (SSL_CTX, urllib.request); тот не
    переписываем (его поведение покрыто тестами), общий шаблон запроса
    вынесен сюда, чтобы не дублировать SSL/таймаут-логику.

    Возвращает ``(status_code | None, json_data | None, err_str | None)``:
    - ``(code, тело, None)`` — сервер ответил (в т.ч. HTTPError 4xx/5xx —
      это НЕ исключение для дыма: код несёт информацию о статусе эндпоинта);
    - ``(None, None, текст)`` — сетевая ошибка/таймаут/битый JSON."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": headers.get("Authorization", ""),
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            **headers,
        },
        method=method,
    )
    try:
        resp = urllib.request.urlopen(
            req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT if timeout is None else timeout
        )
        code = resp.status
        raw = resp.read()
    except urllib.error.HTTPError as e:
        code = e.code
        raw = e.read()
    except Exception as e:
        return None, None, str(e)
    try:
        return code, json.loads(raw), None
    except Exception:
        return code, None, None


def _probe_model(backend: dict, name: str) -> tuple[str | None, bool | None]:
    """Модель, которой пробуется эндпойнт ``name`` бэкенда.

    Политика «только явно указанные пробы» (см. probe_endpoints):
    ``probe`` бэкенда задан и содержит ``name`` с непустой моделью, И эта
    модель есть среди моделей бэкенда в /v1/models → она (model_is_valid
    = True). Все прочие случаи НЕ пробуются:
      - ``probe`` не задан / ``name`` в нём нет / значение пустое
        (``messages:``) → ``(None, None)`` — штатный пропуск эндпойнта
        (молча, без ошибки и WARN: бэкенд просто не перечисляет эту пробу);
      - модель задана, но НЕ найдена среди моделей бэкенда → ``(None,
        False)`` — эндпоинт пропускается ТОЧЕЧНО с текстом в errors
        (валидация строгая: слать бэкенду несуществующую модель бессмысленно).

    У бэкенда нет НИ ОДНОЙ модели в кэше (упал на /v1/models) — probe-модель
    из YAML не с чем сверить: возвращается ``(specified, False)`` для
    заданных проб (пропуск с текстом) и ``(None, None)`` для остальных;
    весь бэкенд без моделей обрабатывается в probe_endpoints (см.
    ``_backend``-ошибку)."""
    probe = backend.get("probe")
    if probe and name in probe and probe[name]:
        specified = probe[name]
        # Сверка с реальностью: модель задана в YAML, но её нет среди
        # моделей бэкенда в /v1/models — эндпоинт НЕ пробуется (валидация
        # строгая: слать бэкенду несуществующую модель бессмысленно).
        if any(
            _MODEL_TO_BACKEND[mid][0] == backend["name"] and mid == specified
            for mid in _MODEL_TO_BACKEND
        ):
            return specified, True
        return None, False
    return None, None


def _pname_for_path(path: str) -> str | None:
    """Короткое имя эндпоинта ENDPOINT_PROBES по полному пути (или None)."""
    for pname, ep_path, _tpl in ENDPOINT_PROBES:
        if ep_path == path:
            return pname
    return None


def _write_endpoint_snapshot(
    bname: str,
    model: str,
    pname: str,
    path: str,
    status: int | None,
    found: bool,
    *,
    error: str | None = None,
) -> None:
    """JSON-дамп результата пробы эндпоинта модели (v0.9.0).

    Файл ``<бэкенд>.<конверт.модель>.<pname>.json`` в ADAPTER_DEBUG_LOGPATH
    (конвертация модели — probe_json._convert_name). Безусловный
    наблюдательный канал: пишется при каждой фактической пробе (фоновая
    проверка бэкендов и пер-модельные пробы usage-таблицы), перезаписывая
    файл целиком. ``status=None`` — сетевая ошибка/таймаут (found=False,
    текст — в ``error``). Провал записи молча глотается в probe_json."""
    payload = {
        "backend": bname,
        "model": model,
        "endpoint": pname,
        "path": path,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": status,
        "found": found,
    }
    if error is not None:
        payload["error"] = error
    probe_json.write_endpoint_json(bname, model, pname, payload)


def probe_endpoints(timeout: float | None = None) -> dict:
    """Дымовая проба API-эндпойнтов настроенных бэкендов.

    Политика «только явно указанные пробы»: пробуется ТОЛЬКО эндпойнт,
    перечисленный в ``probe`` записи backend с НЕПУСТОЙ моделью (этой
    моделью, сверенной со свежим /v1/models). Неперечисленные эндпойнты,
    пустые значения (``messages:``) и бэкенды без ключа ``probe`` вовсе
    НЕ пробуются — штатное состояние (в results не попадают, в errors не
    значатся, лог-строки не дают; у такого бэкенда состояние пробы просто
    НЕ создаётся — колонка «Доступные API» страницы пуста).

    Кэш: при ADAPTER_ENDPOINT_PROBE=0 или свежем результате (моложе
    ENDPOINT_PROBE_TTL) сеть не трогается — возвращается _ENDPOINT_STATE
    как есть (при первом вызове — пустой). Свежесть — по самому старому
    элементу: бэкенд, добавленный позже, пробуется, остальные — из кэша.

    Возвращает ``{"ok": bool, "endpoints": {имя: {путь: {...}}},
    "errors": {имя: текст}}``. ``endpoints`` — только реально пробованные
    пути (пропущенные — политикой или из-за отсутствующей probe-модели — в
    результатах НЕ значатся; текст о непройденной сверке — в ``errors``).
    ok=True — хотя бы один путь реально пробован (или отдан из кэша).
    """
    # --- Кэш: мастер-флаг выключен ИЛИ результат свежий (без сети) ---
    if not ADAPTER_ENDPOINT_PROBE:
        return {"ok": False, "endpoints": _ENDPOINT_STATE, "errors": {}}
    cached = [
        s
        for s in _ENDPOINT_STATE.values()
        if s.get("at") is not None and time.time() - s["at"] < ENDPOINT_PROBE_TTL
    ]
    if len(cached) == len(_ENDPOINT_STATE) and cached:
        return {
            "ok": True,
            "endpoints": _ENDPOINT_STATE,
            "errors": {
                name: s["errors"]
                for name, s in _ENDPOINT_STATE.items()
                if s.get("at") is not None and time.time() - s["at"] < ENDPOINT_PROBE_TTL
            },
        }

    # --- Проба: бэкенды из _BACKENDS (standalone — перечитать YAML) ---
    backends = _BACKENDS
    if not backends:
        blocks = _parse_backend_yaml(ADAPTER_BACKEND_CONFIG)
        if not blocks:
            return {"ok": False, "endpoints": _ENDPOINT_STATE, "errors": {}}
        backends = blocks

    state = _ENDPOINT_STATE
    errors_all: dict[str, str] = {}
    ok_any = False
    for b in backends:
        bname = b["name"]
        # Свежий результат этого бэкенда — из кэша, не пробуем повторно.
        prev = state.get(bname)
        if (
            prev is not None
            and prev.get("at") is not None
            and time.time() - prev["at"] < ENDPOINT_PROBE_TTL
        ):
            ok_any = True
            continue
        # Собираем probes: модель на каждый эндпойнт из ENDPOINT_PROBES,
        # ПЕРЕЧИСЛЕННЫЙ в probe бэкенда с непустой моделью (политика
        # «только явно указанные пробы» — неперечисленные/пустые значения
        # не пробуются вовсе, молча: для них _probe_model вернул (None, None)).
        probes: dict[str, str] = {}
        berrors: dict[str, str] = {}
        for pname, path, _tpl in ENDPOINT_PROBES:
            model, model_valid = _probe_model(b, pname)
            if model is None:
                if model_valid is False:
                    # probe-модель задана в YAML, но НЕ найдена среди моделей
                    # бэкенда в /v1/models — пропускается ТОЛЬКО этот эндпоинт
                    # (остальные пробуются своими моделями); после следующего
                    # refresh_models (модели могли обновиться) — пробуется снова.
                    spec = (b.get("probe") or {}).get(pname, "")
                    print(
                        f"[WARN] Endpoint probe: model '{spec}' for '{pname}' "
                        f"not found among models of backend '{bname}' — "
                        f"skipping this endpoint only"
                    )
                    berrors[pname] = (
                        f"probe model '{spec}' for {pname} not found among backend models"
                    )
                    continue
                # model_valid is None — эндпойнт не перечислен в probe с непустой
                # моделью: штатный пропуск политикой. Но если у бэкенда НЕТ НИ
                # ОДНОЙ модели в кэше (упал на /v1/models), заданные probe-модели
                # не с чем сверить — весь бэкенд в errors, эндпойнты не трогаем.
                if any(_MODEL_TO_BACKEND[mid][0] == bname for mid in _MODEL_TO_BACKEND):
                    continue  # модели есть — обычный политический пропуск
                berrors["_backend"] = (
                    "no models available (backend unreachable or empty /v1/models)"
                )
                break
            probes[path] = model
        if not probes:
            # Бэкенд не перечислил ни одной пробы (нет ключа probe / все
            # значения пустые) — штатно: проб не нужно, состояние НЕ
            # создаётся (колонка «Доступные API» пуста), в errors бэкенд
            # не попадает. Ошибочный пропуск — только когда весь бэкенд
            # без моделей (berrors["_backend"]).
            if berrors:
                errors_all[bname] = "; ".join(f"{k}: {v}" for k, v in berrors.items())
            continue
        result = _probe_backend_endpoints(b, probes, timeout=timeout)
        ok_any = True
        if result["errors"]:
            errors_all[bname] = "; ".join(f"{k}: {v}" for k, v in result["errors"].items())
        elif berrors:
            # Проба прошла без сетевых ошибок, но часть эндпойнтов была
            # пропущена на этапе сбора (probe-модель не найдена) — их тексты
            # живут в berrors и должны попасть в сводку errors бэкенда.
            errors_all[bname] = "; ".join(f"{k}: {v}" for k, v in berrors.items())
        state[bname] = {
            "at": time.time(),
            "endpoints": result["endpoints"],
            "errors": result["errors"],
        }
        _log_probe(bname, b["base"], result["endpoints"], result["errors"])
        # JSON-дампы результатов пробы эндпоинтов (v0.9.0): файл на каждый
        # реально пробованный путь — <бэкенд>.<модель>.<pname>.json, где
        # модель — та, которой эндпоинт пробовался (probes[path]). Пишутся
        # при каждом фактическом прогоне (не из кэша), перезаписываясь
        # целиком. (ep_name, а не pname: внешний pname-цикл выше имеет тип
        # str, тут — str | None после _pname_for_path.)
        for path, ep in result["endpoints"].items():
            ep_name = _pname_for_path(path)
            if ep_name is None:
                continue  # путь вне ENDPOINT_PROBES — не наш (страховка)
            _write_endpoint_snapshot(
                bname,
                probes[path],
                ep_name,
                path,
                ep.get("status"),
                bool(ep.get("found")),
                error=result["errors"].get(ep_name),
            )

    return {
        "ok": ok_any,
        "endpoints": _ENDPOINT_STATE,
        "errors": errors_all,
    }


def _probe_backend_endpoints(backend: dict, probes: dict[str, str], timeout: float | None) -> dict:
    """Одна дымовая проба бэкенда: POST на каждый путь из ENDPOINT_PROBES.

    ``probes`` — подготовленный словарь {путь: модель} (см. probe_endpoints:
    модель на каждый эндпойнт, сверена с /v1/models бэкенда). Возвращает
    ``{"endpoints": {путь: {"status": int|None, "found": bool}},
    "errors": {короткое_имя: текст}}`` — endpoints только для реально
    пробованных путей, классифицированы по HTTP-коду: found=True только
    для 200; любой другой код (400/401/405/429, 5xx, 404) → found=False;
    сеть/таймаут → found=False + текст ошибки в errors."""
    base = backend["base"].rstrip("/")
    key = backend.get("key", "")
    endpoints: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for pname, path, tpl in ENDPOINT_PROBES:
        if path not in probes:
            continue  # эндпоинт пропущен на этапе сбора (нет probe-модели)
        model = probes[path]
        headers = {"Authorization": f"Bearer {key}"}
        body = dict(tpl)
        if pname == "completions":
            body["model"] = model
            body["messages"] = [{"role": "user", "content": "ping"}]
        elif pname == "messages":
            # [AN]-совместимые бэкенды отвечают 400 даже на живом /v1/messages
            # без заголовка версии — шлём его всегда.
            headers["anthropic-version"] = "2023-06-01"
            body["model"] = model
            body["messages"] = [{"role": "user", "content": "ping"}]
        elif pname in ("responses", "embeddings"):
            body["model"] = model
            body["input"] = "ping"
        code, _json, err = _http_json("POST", base + path, headers, body, timeout)
        if err is not None:
            # Сетевая ошибка/таймаут — весь бэкенд недоступен: классифицируем
            # как «не реализован» (found=False) и фиксируем текст ошибки.
            endpoints[path] = {"status": None, "found": False}
            errors[pname] = f"network error: {err}"
            continue
        assert code is not None
        if code == 200:
            endpoints[path] = {"status": code, "found": True}
        else:
            # Не-200 (404, 400/401/405/429, прочие 4xx/5xx): эндпоинт НЕ
            # работает — тело/ключ не подошли либо путь не реализован.
            # На странице такие не показываются (зелёный ✓ — только 200).
            endpoints[path] = {"status": code, "found": False}
    return {"endpoints": endpoints, "errors": errors}


def _log_probe(bname: str, base: str, endpoints: dict[str, dict], errors: dict[str, str]) -> None:
    """Консольный лог-блок [ENDPOINT_PROBE] — одна строка на фактическую
    пробу бэкенда; формат един для лога и страницы (порядок — ENDPOINT_PROBES,
    по коротким именам, сырые HTTP-коды). Кэш-хиты не логируются (спам при
    частых проверках; страница показывает последний результат, лог даёт
    историю фактических проб). Печатается БЕЗУСЛОВНО (консольные debug-логи
    не гейтятся; см. v0.8.6). Содержимое ответов не пишется: дымовые
    запросы, секретов нет."""
    if errors:
        print(f"[ENDPOINT_PROBE] backend '{bname}' ({base}): failed: {errors}")
        return
    parts = []
    for pname, path, _tpl in ENDPOINT_PROBES:
        if path not in endpoints:
            continue
        status = endpoints[path]["status"]
        parts.append(f"{pname}={status if status is not None else 'err'}")
    if parts:
        print(f"[ENDPOINT_PROBE] backend '{bname}' ({base}): {' '.join(parts)}")


def upsert_endpoint_state(backend_name: str, pname: str, status: int | None, found: bool) -> None:
    """Записать результат пробы одного эндпоинта бэкенда в _ENDPOINT_STATE.

    Синхронизация из model_usage (первое обращение к модели, «Перепроверить»
    строки, загрузка model-usage.yaml): эндпоинт, найденный дымовой пробой
    МОДЕЛИ (found=True ⇔ HTTP 200), должен быть виден колонке «Доступные API»
    бэкенда и экспортёру, даже если фоновая проверка бэкенда его не пробовала.
    Сети здесь нет — это перенос уже добытого результата (контракт
    «загруженные строки не перепроверяются» не нарушается).

    ``pname`` — короткое имя эндпоинта из ENDPOINT_PROBES (completions /
    messages / responses / embeddings); неизвестное имя — no-op. Функция
    идемпотентна: повторная запись того же эндпоинта перезаписывает
    результат и освежает ``at`` (кэш TTL). Запись создаётся и для бэкенда,
    отсутствующего в _BACKENDS (такое возможно лишь для осиротевших строк
    usage-таблицы) — фильтр «только настроенные бэкенды» применяет вызывающий.
    """
    path = next((p for n, p, _t in ENDPOINT_PROBES if n == pname), None)
    if path is None:
        return
    state = _ENDPOINT_STATE.setdefault(backend_name, {"at": 0.0, "endpoints": {}, "errors": {}})
    state["endpoints"][path] = {"status": status, "found": found}
    state["at"] = time.time()


def endpoint_support(backend_name: str, pname: str) -> bool | None:
    """Поддерживает ли бэкенд формат ``pname`` — ОТВЕТ ТОЛЬКО ПО КЭШУ ПРОБ.

    Источник — _ENDPOINT_STATE (фоновая probe_endpoints + пер-модельные пробы
    usage-таблицы через upsert_endpoint_state): ``found=True`` (HTTP 200) ⇔
    поддержка. Сети здесь НЕТ — это чистый геттер кэша для роутинга
    (routing.decide), который не делает синхронных проб во время запроса.

    Возвращает:
    - True — бэкенд подтверждённо поддерживает формат (found=True);
    - False — пробовался, но не поддерживает (не-200);
    - None — неизвестно: не пробовался / пробы выключены (ADAPTER_ENDPOINT_
      PROBE=0 и пер-модельные пробы не наполняли кэш) / ``pname`` вне
      ENDPOINT_PROBES. Зовущий (routing.decide) трактует None оптимистично —
      «нет данных → маршрут выбирается, отказ только при found=False»."""
    path = next((p for n, p, _t in ENDPOINT_PROBES if n == pname), None)
    if path is None:
        return None
    ep = _ENDPOINT_STATE.get(backend_name, {}).get("endpoints", {}).get(path)
    if ep is None:
        return None
    return bool(ep.get("found"))


def _init_multi_backends(config_path: str) -> None:
    """Загрузить YAML-конфиг, пробовать модели, построить model → backend map.

    Алгоритм префиксов:
    1) Собрать все model id со всех бэкендов.
    2) Обнаружить коллизии — id, которые встречаются более чем на одном бэкенде.
    3) Для коллизирующих заменить id на ``<backend_name>.<model_id>``.
    4) Для некллизирующих оставить как есть.

    ADAPTER_MODELS_MAPPING не учитывает префиксы — маппинг применяется
    к имени модели до разрешения бэкенда (по lookup в _MODEL_TO_BACKEND)."""
    blocks = _parse_backend_yaml(config_path)
    if blocks is None:
        print(f"[FATAL] Failed to parse backend config: {config_path}")
        sys.exit(1)

    global _BACKENDS, _BACKEND_BY_NAME, _DEFAULT_BACKEND, _AVAILABLE_MODELS, _MODEL_TO_BACKEND

    # Глобалы _AVAILABLE_MODELS/_MODEL_TO_BACKEND ПЕРЕживают переприсваивание:
    # server.py и другие модули делают `from .config import _AVAILABLE_MODELS` на
    # импорте и держат ссылку на ОРИГИНАЛЬНЫЙ объект словаря. Поэтому словари
    # мутируются на месте (clear + update), а не пересоздаются — иначе сервер
    # продолжает видеть пустой/устаревший кэш (501/400 на живых моделях).
    _BACKENDS = blocks
    _BACKEND_BY_NAME = {b["name"]: b for b in blocks}
    _DEFAULT_BACKEND = blocks[0]

    # 1) Собрать все модели: (model_dict_copy, backend_config)
    all_models: list[tuple[dict, dict]] = []
    for b in blocks:
        name, base = b["name"], b["base"]
        # Начало/завершение проверки КАЖДОГО бэкенда из настроек — в лог
        # (при старте адаптера; фоновая проверка WEBUI логируется своими
        # [REFRESH]/[ENDPOINT_PROBE]-строками, см. refresh_models).
        print(f"[INIT] Probing backend '{name}' at {base} ...")
        try:
            bmodels = _fetch_models(base, b["key"])
        except Exception as e:
            print(f"[WARN] Failed to probe backend '{name}' at {base}: {e}")
            _write_models_snapshot(name, None, error=str(e))
            continue
        print(f"[INIT] Backend '{name}' at {base}: ok ({len(bmodels)} models)")
        # JSON-дамп результата стартовой проверки (v0.9.0) — пишется при
        # каждом init, перезаписывая файл целиком.
        _write_models_snapshot(name, bmodels)
        for m in bmodels:
            # Делаем копию, чтобы не мутировать оригинальный ответ бэкенда
            all_models.append((dict(m), b))

    if not all_models:
        print("[FATAL] No models retrieved from any backend — exiting.")
        sys.exit(1)

    available, model_to_backend = _rebuild_index(all_models)
    # Мутация на месте — см. комментарий выше (импортированные ссылки живые).
    _AVAILABLE_MODELS.clear()
    _AVAILABLE_MODELS.update(available)
    _MODEL_TO_BACKEND.clear()
    _MODEL_TO_BACKEND.update(model_to_backend)

    print(f"[INIT] Loaded {len(_AVAILABLE_MODELS)} models from {len(blocks)} backends")
    for mid in sorted(_AVAILABLE_MODELS.keys()):
        print(f"  model={mid}")


def _rebuild_index(all_models: list[tuple[dict, dict]]) -> tuple[dict[str, dict], dict[str, tuple]]:
    """Построить (_AVAILABLE_MODELS, _MODEL_TO_BACKEND) из ``all_models``.

    ``all_models`` — список ``(модель, backend_config)``, где модель — уже
    копия (``dict(m)``), чтобы переименование при коллизии не мутировало
    оригинальный ответ бэкенда.

    Алгоритм префиксов (общий для init и refresh):
    1) Собрать все model id со всех бэкендов.
    2) Обнаружить коллизии — id, встречающиеся более чем на одном бэкенде.
    3) Для коллизирующих заменить id на ``<backend_name>.<model_id>``.
    4) Для некколлизирующих оставить как есть.

    Возвращает новые словари (глобалы обновляет вызывающий)."""
    id_counter: dict[str, int] = {}
    for m, _ in all_models:
        mid = m.get("id", "")
        id_counter[mid] = id_counter.get(mid, 0) + 1
    colliding_ids = {k for k, v in id_counter.items() if v > 1}

    available: dict[str, dict] = {}
    model_to_backend: dict[str, tuple] = {}
    for m, backend in all_models:
        mid = m.get("id", "")
        if mid in colliding_ids:
            prefixed = f"{backend['name']}.{mid}"
            m["id"] = prefixed
            available[prefixed] = m
            model_to_backend[prefixed] = (backend["name"], backend)
        else:
            available[mid] = m
            model_to_backend[mid] = (backend["name"], backend)
    return available, model_to_backend


def reload_backend_config() -> list[dict] | None:
    """Перечитать ADAPTER_BACKEND_CONFIG и подменить глобалы бэкендов.

    Кнопка 🔃 «Перепроверить бэкенды» на статус-странице должна не только
    перепроверять все настроенные бэкенды, но и заново читать YAML-конфиг — добавление/
    удаление бэкендов работает БЕЗ рестарта адаптера. Парсер и без того
    выбирает только ключ ``backend`` (прочие ключи файла игнорируются).

    Возвращает список новых блоков при успехе; None — файл битый/
    недоступный/пустой: глобалы бэкендов НЕ трогаются (решение о дальнейших
    действиях — за вызывающим: start_refresh логирует [WARN] и продолжает
    фоновую проверку прежних бэкендов).

    Меняет ТОЛЬКО бэкенды и probe-кэш:
      - ``_BACKENDS``/``_BACKEND_BY_NAME``/``_DEFAULT_BACKEND`` — подменяются
        целиком (читаются как атрибуты модуля — переприсваивание допустимо);
      - ``_ENDPOINT_STATE`` — stale-очистка: записи бэкендов, которых нет в
        новом списке, удаляются (не висят результаты проб удалённых
        бэкендов); свежие записи оставшихся сохраняются (кэш TTL 60 с не
        сбрасывается без нужды);
      - модели/индексы (``_AVAILABLE_MODELS``/``_MODEL_TO_BACKEND``) НЕ
        трогаются — их пересоберёт refresh_models по новому списку бэкендов
        (in-place clear+update уже реализован там). Этот вызов — только про
        бэкенды; мутация словарей-кэшей на месте — в refresh_models."""
    blocks = _parse_backend_yaml(ADAPTER_BACKEND_CONFIG)
    if not blocks:
        return None
    global _BACKENDS, _BACKEND_BY_NAME, _DEFAULT_BACKEND
    _BACKENDS = blocks
    _BACKEND_BY_NAME = {b["name"]: b for b in blocks}
    _DEFAULT_BACKEND = blocks[0]
    # Stale-очистка probe-кэша: результаты проб бэкендов, которых больше нет
    # в конфиге, удаляются (импортированная по ссылке _ENDPOINT_STATE
    # мутируется на месте — переприсваивание недопустимо, см. её комментарий).
    for name in list(_ENDPOINT_STATE):
        if name not in _BACKEND_BY_NAME:
            del _ENDPOINT_STATE[name]
    print(f"[BACKEND_CONFIG] Reloaded {len(blocks)} backend(s) from {ADAPTER_BACKEND_CONFIG}")
    return blocks


def refresh_models(timeout: float | None = None) -> dict:
    """Пере-опросить бэкенды и обновить кэш моделей ``_AVAILABLE_MODELS`` /
    ``_MODEL_TO_BACKEND``.

    Вызывается только по явному сигналу: при старте адаптера (существующий
    init/probe), из фонового воркера проверки (config.start_refresh — старт
    адаптера / первый GET "/" статус-страницы WEBUI / кнопка 🔃
    «Перепроверить бэкенды»).
    Периодического фонового обновления НЕТ. Бэкенд может добавлять модели
    между стартами; refresh подхватывает их без перезапуска адаптера.

    ``timeout`` — таймаут на один бэкенд (None → ADAPTER_TIMEOUT). Страница
    статуса передаёт короткий (PROBE_TIMEOUT), чтобы не висеть по 300 с.

    Основной путь — в процессе адаптера: ``_BACKENDS`` заполнен при старте;
    refresh опрашивает каждый бэкенд и пересобирает оба словаря.
    Если ``_BACKENDS`` пуст (viewer вне адаптера): блоки YAML перечитываются
    из ADAPTER_BACKEND_CONFIG, бэкенды опрашиваются — кнопка 🔃
    «Перепроверить бэкенды» работает и без процесса адаптера.

    Возвращает ``{"ok": bool, "count": int, "errors": {имя_бэкенда: текст}}``:
    - ``ok=True`` — кэш пересобран из ответивших бэкендов. При частичном
      успехе модели упавших бэкендов выпадают из кэша (бэкенд недоступен —
      это честное состояние); текст ошибок — в ``errors``.
    - ``ok=False`` — ни один бэкенд не ответил (или нет ни одного
      настроенного бэкенда: standalone без env); старый кэш НЕ тронут,
      ``count`` — размер прежнего списка (на странице показывается он)."""
    global _AVAILABLE_MODELS, _MODEL_TO_BACKEND, _BACKENDS, _BACKEND_BY_NAME, _DEFAULT_BACKEND

    # Опрашиваем каждый бэкенд, ошибки копим по имени. _BACKENDS может быть
    # пуст — standalone: перечитываем YAML (тот же путь, что адаптер взял бы
    # при старте), чтобы кнопка работала.
    backends = _BACKENDS
    if not backends:
        # standalone: адаптер в этом процессе не инициализировался —
        # поднимаем глобалы бэкендов из YAML, чтобы _collect_endpoints
        # нашёл эндпоинты, а _resolve_backend маршрутизировал запросы.
        blocks = _parse_backend_yaml(ADAPTER_BACKEND_CONFIG)
        if not blocks:
            # Env не задаёт ни одного бэкенда — обновлять нечего.
            return {"ok": False, "count": len(_AVAILABLE_MODELS), "errors": {}}
        backends = blocks
        _BACKENDS = blocks
        _BACKEND_BY_NAME = {b["name"]: b for b in blocks}
        _DEFAULT_BACKEND = blocks[0]
    all_models: list[tuple[dict, dict]] = []
    errors: dict[str, str] = {}
    for b in backends:
        bname = b["name"]
        try:
            bmodels = _fetch_models(b["base"], b["key"], timeout=timeout)
        except Exception as e:
            errors[bname] = str(e)
            # JSON-дамп результата проверки упавшего бэкенда (v0.9.0).
            _write_models_snapshot(bname, None, error=str(e))
            continue
        # JSON-дамп результата фоновой проверки бэкенда (v0.9.0) — пишется
        # при каждом refresh, перезаписывая файл целиком.
        _write_models_snapshot(bname, bmodels)
        for m in bmodels:
            all_models.append((dict(m), b))

    if not all_models:
        return {"ok": False, "count": len(_AVAILABLE_MODELS), "errors": errors}

    available, model_to_backend = _rebuild_index(all_models)
    # Мутация на месте — см. комментарий в _init_multi_backends: модули,
    # импортировавшие глобалы (server.py и др.), держат ссылку на исходные
    # объекты словарей и должны видеть обновлённый кэш.
    _AVAILABLE_MODELS.clear()
    _AVAILABLE_MODELS.update(available)
    _MODEL_TO_BACKEND.clear()
    _MODEL_TO_BACKEND.update(model_to_backend)
    print(f"[REFRESH] Reloaded {len(_AVAILABLE_MODELS)} models from {len(backends)} backends")
    result = {"ok": True, "count": len(_AVAILABLE_MODELS), "errors": errors}
    # Дымовая проба API-эндпойнтов — ПОСЛЕ обновления кэша моделей (модели из
    # YAML-ключа probe сверяются со свежим /v1/models). Результат добавляется
    # ключом "probe" — старые читатели полей ok/count/errors не ломаются.
    result["probe"] = probe_endpoints(timeout=timeout)
    return result


# ==================== MULTI-BACKEND: ФОНОВАЯ ПРОВЕРКА ====================
# Статус-страница WEBUI запускает refresh_models (модели + дымовая проба
# эндпоинтов) в фоновом потоке, чтобы HTTP-ответ "/" не висел, пока бэкенды
# опрашиваются. Состояние проверки — модульный снимок-словарь _REFRESH_JOB,
# заменяемый ЦЕЛИКОМ (никогда не мутируется после публикации): читатели
# (webui_status) берут refresh_state() без лока — замена ссылки атомарна.
# Запуск сериализуется _REFRESH_LOCK: две кнопки подряд не создадут два потока.
# Периодического фонового refresh нет — проверка только по явному
# start_refresh(): при старте адаптера, на первом GET "/" (автостарт) и по
# кнопке 🔃 «Перепроверить бэкенды» (POST "/").

_REFRESH_JOB: dict | None = None
_REFRESH_LOCK = threading.Lock()


def _refresh_state_default() -> dict:
    """Состояние «проверок ещё не было» — дефолт для refresh_state()."""
    return {
        "running": False,
        "started_at": None,
        "done_at": None,
        "ok": None,
        "count": None,
        "providers": None,
        "errors": None,
        "checked_at": None,
    }


def _refresh_worker(timeout: float | None) -> None:
    """Тело фонового потока проверки: refresh_models + публикация результата.

    refresh_models сам мутирует конфиг-глобалы (_AVAILABLE_MODELS/
    _MODEL_TO_BACKEND/_ENDPOINT_STATE) — это уже было при синхронном вызове
    со страницы, отдельный Lock вокруг них не добавляем (чтение словарей из
    HTTP-потоков прокси при clear+update — существующий компромисс проекта).

    ``finally`` гарантирует публикацию running=False даже при исключении
    (проверка не может «зависнуть навсегда»: refresh_models ограничен
    таймаутами запросов). Исключение логируется в состояние, а не роняет
    поток."""
    result = None
    error_text = None
    try:
        result = refresh_models(timeout=timeout)
    except Exception as e:  # noqa: BLE001 — воркер не должен ронять поток
        error_text = str(e)
    global _REFRESH_JOB
    snapshot = _refresh_state_default()
    snapshot["running"] = False
    snapshot["started_at"] = (_REFRESH_JOB or {}).get("started_at")
    snapshot["done_at"] = time.time()
    if error_text is not None:
        snapshot["errors"] = {"__worker__": error_text}
        snapshot["ok"] = False
        snapshot["count"] = (_REFRESH_JOB or {}).get("count")
        print(f"[REFRESH] Worker failed: {error_text}")
    elif result is not None:
        snapshot["ok"] = result.get("ok")
        snapshot["count"] = result.get("count")
        snapshot["errors"] = result.get("errors")
    # Число настроенных бэкендов на момент финальной публикации — для футера
    # «(N провайдеров, M моделей)» (см. webui_status). reload_backend_config
    # мог поменять _BACKENDS до запуска refresh_models; standalone без конфига
    # — 0. refresh_state() возвращает снимок, а не живой len(_BACKENDS).
    snapshot["providers"] = len(_BACKENDS)
    snapshot["checked_at"] = time.strftime("%H:%M:%S")
    _REFRESH_JOB = snapshot


def start_refresh(timeout: float | None = None, reload: bool = True) -> bool:
    """Запустить фоновую проверку бэкендов (модели + проба эндпоинтов).

    ``reload=True`` (по умолчанию) — перед проверкой конфиг
    ADAPTER_BACKEND_CONFIG перечитывается (reload_backend_config): кнопка 🔃
    «Перепроверить бэкенды» добавляет/удаляет бэкенды БЕЗ рестарта адаптера.
    Битый/недоступный YAML при reload — прежние бэкенды остаются ([WARN]),
    фоновая проверка всё равно перепроверяет их (reload_backend_config
    вернул None — глобалы не тронуты; refresh_models идёт по прежним).

    Возвращает True, если проверка запущена этим вызовом; False — если она
    уже выполняется (повторный запуск не создаёт второй поток). HTTP-ответ
    страницы не блокируется: поток daemon, результат появится в состоянии
    (refresh_state) по завершении. Никакого периодического refresh — только
    явный вызов: старт адаптера (backend-adapter.py), первый GET "/"
    (автостарт, webui_status._autostart_first_check) или кнопка 🔃
    «Перепроверить бэкенды» (POST "/")."""
    global _REFRESH_JOB
    if reload and reload_backend_config() is None:
        print(
            f"[WARN] Backend config reload failed ({ADAPTER_BACKEND_CONFIG}) — "
            "keeping current backends"
        )
    with _REFRESH_LOCK:
        if _REFRESH_JOB is not None and _REFRESH_JOB.get("running"):
            return False
        snapshot = _refresh_state_default()
        snapshot["running"] = True
        snapshot["started_at"] = time.time()
        _REFRESH_JOB = snapshot
        threading.Thread(target=_refresh_worker, args=(timeout,), daemon=True).start()
        return True


def stop_refresh(timeout: float = 2.0) -> None:
    """Дождаться завершения идущей фоновой проверки бэкендов (до timeout).

    Вызывается при вежливом завершении адаптера (Ctrl-C/SIGTERM): если
    refresh_worker в процессе сетевого опроса, ждём его завершения, чтобы
    снимок состояния (refresh_state) и кэши моделей/эндпоинтов были
    консистентны на момент выхода, а консоль не обрывалась посреди
    [REFRESH]/[ENDPOINT_PROBE]-строк. Поток daemon: если не успел за
    timeout, выходим без ожидания — процесс завершится сам, воркер оборвётся.
    Никаких флагов остановки воркеру не передаётся (сеть ограничена
    таймаутами запросов; дождаться текущей итерации — достаточная
    вежливость)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _REFRESH_LOCK:
            running = _REFRESH_JOB is not None and _REFRESH_JOB.get("running")
        if not running:
            return
        time.sleep(0.05)


def refresh_state() -> dict:
    """Снимок состояния проверки (копия — мутация результата безопасна).

    Поля: running (идёт ли проверка), started_at/done_at (time.time()),
    ok/count/providers/errors — итог последней проверки refresh_models
    (providers — число настроенных бэкендов на момент публикации, для
    футера страницы), checked_at — "HH:MM:SS" её завершения. До первой
    проверки — дефолт (все None/False)."""
    job = _REFRESH_JOB
    if job is None:
        return _refresh_state_default()
    return dict(job)


# ==================== MULTI-BACKEND: ROUTING ====================
def _resolve_backend(model: str) -> tuple[dict, str]:
    """Определить целевой бэкенд для модели.

    Возвращает ``(backend_cfg, resolved_model_name)``.

    Логика:
    1. Явный префикс ``<backend_name>.<model>`` → stripping, routing.
    2. Lookup в ``_MODEL_TO_BACKEND`` → первый найденный бэкенд.
    3. Fallback → ``_DEFAULT_BACKEND``.
    """
    # 1) Явный префикс: модель начинается с имени одного из бэкендов + '.'
    for bname, bcfg in _BACKEND_BY_NAME.items():
        prefix = bname + "."
        if model.startswith(prefix):
            actual = model[len(prefix) :]
            return bcfg, actual

    # 2) Lookup по известному списку
    entry = _MODEL_TO_BACKEND.get(model)
    if entry:
        # Если модель prefixed (имя из colliding_ids) — stripping префикса:
        # "kl.qwen3.6-35b-a3b" → "qwen3.6-35b-a3b"
        actual = model
        for bname in _BACKEND_BY_NAME:
            prefix = bname + "."
            if model.startswith(prefix):
                actual = model[len(prefix) :]
                break
        return entry[1], actual

    # 3) Fallback
    if _DEFAULT_BACKEND:
        return _DEFAULT_BACKEND, model
    # Должно быть недостижимо при корректном старте (пустой конфиг — fatal,
    # см. startup), но на случай неожиданных путей — явная ошибка, а не
    # тихий fallback на несуществующий конфиг.
    raise RuntimeError(f"no backend resolved for model {model!r}")
