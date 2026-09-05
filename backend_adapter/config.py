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

# ==================== НАСТРОЙКИ ====================
PROXY_PORT = int(os.environ.get("ADAPTER_PROXY_PORT", "9999"))
# Адрес (host), на котором слушает HTTP-эндпоинт адаптера. Пусто / не задано —
# дефолт 127.0.0.1 (localhost). "0.0.0.0" — слушать на всех интерфейсах
# (доступ из сети). Идиом `or "127.0.0.1"`, а не get(name, default): пустая
# env-переменная в bind-кортеже socketserver означала бы INADDR_ANY (все
# интерфейсы) — с `or` и незаданная, и пустая дают безопасный localhost.
ADAPTER_ENDPOINT_HOST = os.environ.get("ADAPTER_ENDPOINT_HOST", "") or "127.0.0.1"
ADAPTER_DEBUG = os.environ.get("ADAPTER_DEBUG_ENABLE", "1").lower() not in ("0", "false", "no", "")
# Единый путь к ДИРЕКТОРИИ логов сессий (debug-логи, trace, *.parts дампы).
# ПУСТО / не задано → файловая запись ВЫКЛЮЧЕНА (zero-config: консольные
# debug-блоки видны, ничего не пишется на диск и папка не создаётся).
# Задано → директория логов: создаётся при необходимости на старте,
# вся файловая запись (в т.ч. trace и *.parts дампы) подчинена
# ADAPTER_DEBUG_ENABLE (мастер-выключатель: 0 → ничего не пишется).
# Корень веб-интерфейса НЕ зависит от этой переменной (см. блок WEBUI).
# Режим «один файл» удалён — путь всегда директория.
ADAPTER_DEBUG_LOGPATH = os.environ.get("ADAPTER_DEBUG_LOGPATH", "")
ADAPTER_DETACH = os.environ.get("ADAPTER_DETACH_ENABLE", "0").lower() in ("1", "true", "yes")
ADAPTER_TIMEOUT = int(os.environ.get("ADAPTER_TIMEOUT", "300"))
ADAPTER_RETRY = int(os.environ.get("ADAPTER_RETRY_COUNT", "3"))
ADAPTER_DEBUG_TRIM = int(os.environ.get("ADAPTER_DEBUG_TRIM", "3000"))
# Логгирование результатов работы инструментов (обработка собранных логов —
# по умолчанию выключена, zero-config: ничего лишнего не обрабатывается):
#   ADAPTER_DEBUG_TOOLS=1 — писать все результаты ([TOOL_RESULT]).
#   ADAPTER_DEBUG_TOOLS_ERROR=1 — писать ошибки инструментов ([TOOL_RESULT_ERROR]).
#   По умолчанию оба выключены (0).
#   ADAPTER_DEBUG_TAGS_FULL — перечисление тегов через запятую, для которых
#   отключается обрезка (trim). Если тэг в списке — полный вывод без обрезки.
#   Пример: BODY,OPENAI_BODY,FETCH_RAW,TOOL_RESULT,TOOL_RESULT_ERROR,RESPONSE
ADAPTER_DEBUG_TOOLS = os.environ.get("ADAPTER_DEBUG_TOOLS", "0").lower() not in (
    "0",
    "false",
    "no",
    "",
)
ADAPTER_DEBUG_TOOLS_ERROR = os.environ.get("ADAPTER_DEBUG_TOOLS_ERROR", "0").lower() not in (
    "0",
    "false",
    "no",
    "",
)
ADAPTER_TRACE_REASONING_MAX_CHARS = int(os.environ.get("ADAPTER_TRACE_REASONING_MAX_CHARS", "0"))
ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS = int(os.environ.get("ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS", "0"))
# ADAPTER_DEBUG_TAGS_FULL — перечисление тегов через запятую, для которых
# отключается обрезка (trim). Пусто / не задано — trim включён везде.
_ADAPTER_DEBUG_TAGS_FULL_RAW = os.environ.get("ADAPTER_DEBUG_TAGS_FULL", "")
ADAPTER_DEBUG_TAGS_FULL: list[str] = (
    [t.strip() for t in _ADAPTER_DEBUG_TAGS_FULL_RAW.split(",") if t.strip()]
    if _ADAPTER_DEBUG_TAGS_FULL_RAW.strip()
    else []
)
_ADAPTER_DEBUG_TAGS_FULL_SET: frozenset[str] = frozenset(ADAPTER_DEBUG_TAGS_FULL)


def _trim_limit(tag: str) -> int | None:
    """Return None if trim is OFF for this tag, or ADAPTER_DEBUG_TRIM if ON."""
    if tag in _ADAPTER_DEBUG_TAGS_FULL_SET:
        return None
    return ADAPTER_DEBUG_TRIM


ADAPTER_STRICT_MODELS = os.environ.get("ADAPTER_STRICT_MODELS", "1").lower() in ("1", "true", "yes")
# ADAPTER_DEBUG_TAGS_OUT — логический флаг: включить per-session дампы
# (.json и .yaml парой) для всех частей протокола обмена (список частей
# фиксирован — ADAPTER_DEBUG_TAGS_OUT_ALL). Срабатывает только при
# ADAPTER_DEBUG_ENABLE=1, когда ADAPTER_DEBUG_LOGPATH задаёт директорию
# (файлы кладутся в неё). Пусто / 0 / false — выкл.
ADAPTER_DEBUG_TAGS_OUT = os.environ.get("ADAPTER_DEBUG_TAGS_OUT", "").lower() not in (
    "0",
    "false",
    "no",
    "",
)
# Полный фиксированный список частей протокола, для которых пишутся дампы.
ADAPTER_DEBUG_TAGS_OUT_ALL = "BODY,OPENAI_BODY,FETCH_RAW,TOOL_RESULT_ERROR,TOOL_RESULT,RESPONSE"

# ==================== RUNTIME-ПЕРЕКЛЮЧАЕМЫЙ ПУЛ (см. /config эндпойнт) ====================
# Подмножество переменных выше, которые можно менять НЕ ПЕРЕЗАПУСКАЯ адаптер —
# через HTTP API /config (webui_config_api.py), эндпойнт общего WEBUI-сервера
# (тот всегда поднят, если ADAPTER_WEBUI_ENABLE=1 — см. backend-adapter.py).
# Идея: включать накопление логов/трейсов/*.parts-дампов на время диагностики
# конкретной проблемы и выключать обратно, без остановки самого прокси.
#
# Пул НАМЕРЕННО узкий — только то, что управляет ОБЪЁМОМ записи на диск
# (логи/трейсы/дампы). Сеть, бэкенды, модели, порты сюда не входят — их
# runtime-переключение существенно рискованнее (сорвёт активные соединения,
# сменит маршрутизацию на лету) и не было целью этой задачи.
#
# ВАЖНО для того, кто ЧИТАЕТ эти переменные в других модулях: все места
# использования (server.py, streaming.py, convert.py, tracer.py, logger.py)
# переведены на "живое" чтение через `config.ADAPTER_X`, а НЕ через
# `from .config import ADAPTER_X` на уровне модуля — второе сделало бы
# разовый снимок при импорте, и set_runtime_config() ниже не имел бы эффекта
# нигде, кроме этого файла. Если добавляете сюда новую переменную — проверьте
# ВСЕ её точки чтения на этот же паттерн.
#
# ПРИМЕЧАНИЕ: ADAPTER_DEBUG_LOGPATH сюда сознательно НЕ входит — включение
# записи "с нуля" (когда путь изначально пуст) потребовало бы ещё и создать
# директорию/пересоздать session_log._DEBUG_IS_DIR и т.п. на лету, это уже не
# "переключатель объёма", а смена самой точки хранения — за рамками задачи.
# Если ADAPTER_DEBUG_LOGPATH изначально не задан, ADAPTER_DEBUG_TAGS_OUT/
# ADAPTER_DEBUG=1 через /config ничего на диск не запишут (некуда), только
# в консоль — это ожидаемо, а не баг.
RUNTIME_CONFIG_POOL = (
    "ADAPTER_DEBUG",
    "ADAPTER_DEBUG_TAGS_OUT",
    "ADAPTER_DEBUG_TOOLS",
    "ADAPTER_DEBUG_TOOLS_ERROR",
    "ADAPTER_TRACE_REASONING_MAX_CHARS",
    "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS",
    "ADAPTER_DEBUG_TRIM",
)

# Типы для валидации входа /config (POST) — bool или int, остальное отклоняем.
_RUNTIME_CONFIG_TYPES = {
    "ADAPTER_DEBUG": bool,
    "ADAPTER_DEBUG_TAGS_OUT": bool,
    "ADAPTER_DEBUG_TOOLS": bool,
    "ADAPTER_DEBUG_TOOLS_ERROR": bool,
    "ADAPTER_TRACE_REASONING_MAX_CHARS": int,
    "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS": int,
    "ADAPTER_DEBUG_TRIM": int,
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
    и их импортируют по ссылке в других модулях; здесь же пул — bool/int
    скаляры, которые в Python в принципе нельзя мутировать на месте, поэтому
    единственный рабочий вариант — переприсваивание через `global` ЗДЕСЬ, в
    сочетании с тем, что все читатели переведены на live-доступ `config.X`
    (см. комментарий над RUNTIME_CONFIG_POOL).

    Неизвестные ключи и ключи вне пула ИГНОРИРУЮТСЯ МОЛЧА (не 400 — иначе
    один опечатанный лишний ключ в теле запроса откатил бы все остальные
    валидные изменения; вызывающий (webui_config_api.py) сверяет ответ с тем,
    что послал, и сам решает, как об этом сообщить). Значение неверного типа
    для известного ключа — тоже игнорируется (не применяется), остальные
    ключи всё равно применяются. Возвращает get_runtime_config() ПОСЛЕ
    применения — вызывающий видит, что реально изменилось.
    """
    global ADAPTER_DEBUG, ADAPTER_DEBUG_TAGS_OUT, ADAPTER_DEBUG_TOOLS
    global ADAPTER_DEBUG_TOOLS_ERROR, ADAPTER_TRACE_REASONING_MAX_CHARS
    global ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS, ADAPTER_DEBUG_TRIM

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
        globals()[name] = value

    return get_runtime_config()


# ===================================================

# Веб-интерфейс — общий статус адаптера + просмотр сессий:
#   ADAPTER_WEBUI_ENABLE=1 (по умолчанию) — поднять локальный веб-сервер;
#   статус-страница "/" (версия, режим, LLM-эндпоинты) работает всегда,
#   "/session" (просмотр *.parts сессий) — только когда задан
#   ADAPTER_DEBUG_LOGPATH (иначе вкладки сессий пусты, т.к. логи не пишутся).
#   Корень — директория ADAPTER_DEBUG_LOGPATH, если задана; иначе —
#   "./tmp/webui" (отдельная папка, НЕ зависит от лог-директории).
#   Порт — ADAPTER_WEBUI_PORT; адрес — ADAPTER_WEBUI_HOST (пусто/не задано →
#   дефолт 127.0.0.1, только локально).
ADAPTER_WEBUI_ENABLE = os.environ.get("ADAPTER_WEBUI_ENABLE", "1").lower() not in (
    "0",
    "false",
    "no",
    "",
)
ADAPTER_WEBUI_PORT = int(os.environ.get("ADAPTER_WEBUI_PORT", "8765"))
# Адрес, на котором слушает веб-интерфейс; дефолт 127.0.0.1 (только
# локально). "0.0.0.0" — доступ из сети (внимание: содержимое сессий —
# git log, файлы, reasoning — не должно случайно утечь).
ADAPTER_WEBUI_HOST = os.environ.get("ADAPTER_WEBUI_HOST", "") or "127.0.0.1"
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

    ``probe`` — необязательный ключ записи: какие модели использовать при
    «дымовой» пробе API-эндпойнтов бэкенда (см. probe_endpoints). Список пар
    ``<эндпойнт>: <модель>``; пустое значение (``messages:``) и не перечисленные
    эндпойнты пробуются моделью по умолчанию. Порядок пар сохраняется.

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
            # (значение может быть пустым — «модель по умолчанию»; имя
            # эндпойнта — слово, возможно с дефисами).
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


# ==================== MULTI-BACKEND: ENDPOINT PROBE ====================
# «Дымовая» проба API-эндпойнтов бэкенда: короткий POST (max_tokens:1) на
# каждый известный путь — определить, какие эндпойнты бэкенд реально
# обслуживает, не тратя токены на содержательный ответ. Выполняется при
# каждом refresh_models — а тот вызывается из фонового воркера проверки
# (start_refresh: при старте адаптера, на первом GET "/" и по кнопке
# «⟳ Проверить сейчас»), не при каждой загрузке страницы; результат —
# колонка «Доступные API» на странице и лог-строка [ENDPOINT_PROBE] в
# консоли. Мастер-флаг — ADAPTER_ENDPOINT_PROBE (0 — автопроба отключена).
#
# Классификация по HTTP-коду ответа (см. tmp/plan-llm-endpoints.md):
#   200                  — эндпоинт работает (found=True);
#   прочие 4xx/5xx (вкл. 400/401/405/429) и 404 — эндпоинт НЕ работает
#                         (found=False): тело/ключ не подошли, либо путь
#                         не реализован — на странице такие не показываются;
#   сеть/таймаут         — ошибка бэкенда целиком (found=False + текст).
# Пары в YAML-конфиге (`probe` в записи backend) задают, КАКОЙ моделью
# пробовать каждый эндпойнт; неперечисленные/пустые — моделью по умолчанию.
# Модели, не найденные среди моделей бэкенда в /v1/models, пропускаются
# точечно (см. probe_endpoints).
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


def _default_probe_model(backend: dict) -> str | None:
    """Модель по умолчанию для пробы бэкенда — первая из /v1/models бэкенда.

    None — у бэкенда нет моделей в кэше (не опрошен/упал на /v1/models):
    пробовать нечем, эндпойнты такого бэкенда пропускаются целиком."""
    for mid in _MODEL_TO_BACKEND:
        if _MODEL_TO_BACKEND[mid][0] == backend["name"]:
            return mid
    return None


def _probe_model(backend: dict, name: str) -> tuple[str | None, bool | None]:
    """Модель, которой пробуется эндпойнт ``name`` бэкенда.

    Порядок выбора (см. probe_endpoints):
    1) ``probe`` бэкенда задан и содержит ``name`` с непустой моделью,
       И эта модель есть среди моделей бэкенда в /v1/models → она;
       задана, но НЕ найдена — тоже None (эндпоинт пропускается точечно,
       текст про probe-модель идёт в errors);
    2) иначе — модель по умолчанию (первая из /v1/models этого бэкенда);
    3) моделей у бэкенда нет вовсе → None (бэкенд пропускается целиком).

    Возвращает ``(model | None, model_is_valid | None)``: вторая часть —
    True, когда probe-модель задана и валидна; False, когда задана, но НЕ
    найдена среди моделей бэкенда (для текста ошибки); None — probe-модель
    не задана (решение по умолчанию, отдельная сверка не нужна)."""
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
    return _default_probe_model(backend), None


def probe_endpoints(timeout: float | None = None) -> dict:
    """Дымовая проба API-эндпойнтов настроенных бэкендов.

    Вызывается из refresh_models ПОСЛЕ обновления кэша моделей — поэтому
    probe-модели из YAML сверяются со свежим /v1/models этой же пробы.

    Кэш: при ADAPTER_ENDPOINT_PROBE=0 или свежем результате (моложе
    ENDPOINT_PROBE_TTL) сеть не трогается — возвращается _ENDPOINT_STATE
    как есть (при первом вызове — пустой). Свежесть — по самому старому
    элементу: бэкенд, добавленный позже, пробуется, остальные — из кэша.

    Возвращает ``{"ok": bool, "endpoints": {имя: {путь: {...}}},
    "errors": {имя: текст}}``. ``endpoints`` — только реально пробованные
    пути (пропущенные из-за отсутствующей probe-модели в результатах НЕ
    значатся, текст — в ``errors``). ok=True — хотя бы один путь реально
    пробован (или отдан из кэша).
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
        # Собираем probes: модель на каждый эндпойнт из ENDPOINT_PROBES.
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
                # У бэкенда нет НИ ОДНОЙ модели в кэше (упал на /v1/models):
                # пробовать нечем — весь бэкенд в errors, эндпойнты не трогаем.
                berrors["_backend"] = (
                    "no models available (backend unreachable or empty /v1/models)"
                )
                break
            probes[path] = model
        if not probes:
            # Весь бэкенд пропущен (нет моделей) — никаких запросов к нему.
            if berrors:
                errors_all[bname] = "; ".join(f"{k}: {v}" for k, v in berrors.items())
            else:
                errors_all[bname] = "no models to probe with"
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
    историю фактических проб). Гейт — ADAPTER_DEBUG (живое чтение, как
    _d() в logger.py; config.py не может импортировать logger — цикл).
    Содержимое ответов не пишется: дымовые запросы, секретов нет."""
    if not ADAPTER_DEBUG:
        return
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
        try:
            bmodels = _fetch_models(b["base"], b["key"])
        except Exception as e:
            print(f"[WARN] Failed to probe backend '{b['name']}' at {b['base']}: {e}")
            continue
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


def refresh_models(timeout: float | None = None) -> dict:
    """Пере-опросить бэкенды и обновить кэш моделей ``_AVAILABLE_MODELS`` /
    ``_MODEL_TO_BACKEND``.

    Вызывается только по явному сигналу: при старте адаптера (существующий
    init/probe), из фонового воркера проверки (config.start_refresh — старт
    адаптера / первый GET "/" статус-страницы WEBUI / кнопка
    «⟳ Проверить сейчас»).
    Периодического фонового обновления НЕТ. Бэкенд может добавлять модели
    между стартами; refresh подхватывает их без перезапуска адаптера.

    ``timeout`` — таймаут на один бэкенд (None → ADAPTER_TIMEOUT). Страница
    статуса передаёт короткий (PROBE_TIMEOUT), чтобы не висеть по 300 с.

    Основной путь — в процессе адаптера: ``_BACKENDS`` заполнен при старте;
    refresh опрашивает каждый бэкенд и пересобирает оба словаря.
    Если ``_BACKENDS`` пуст (viewer вне адаптера): блоки YAML перечитываются
    из ADAPTER_BACKEND_CONFIG, бэкенды опрашиваются — кнопка
    «⟳ Проверить сейчас» работает и без процесса адаптера.

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
        try:
            bmodels = _fetch_models(b["base"], b["key"], timeout=timeout)
        except Exception as e:
            errors[b["name"]] = str(e)
            continue
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
# кнопке «⟳ Проверить сейчас» (POST "/").

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
    snapshot["checked_at"] = time.strftime("%H:%M:%S")
    _REFRESH_JOB = snapshot


def start_refresh(timeout: float | None = None) -> bool:
    """Запустить фоновую проверку бэкендов (модели + проба эндпоинтов).

    Возвращает True, если проверка запущена этим вызовом; False — если она
    уже выполняется (повторный запуск не создаёт второй поток). HTTP-ответ
    страницы не блокируется: поток daemon, результат появится в состоянии
    (refresh_state) по завершении. Никакого периодического refresh — только
    явный вызов: старт адаптера (backend-adapter.py), первый GET "/"
    (автостарт, webui_status._autostart_first_check) или кнопка
    «⟳ Проверить сейчас» (POST "/")."""
    global _REFRESH_JOB
    with _REFRESH_LOCK:
        if _REFRESH_JOB is not None and _REFRESH_JOB.get("running"):
            return False
        snapshot = _refresh_state_default()
        snapshot["running"] = True
        snapshot["started_at"] = time.time()
        _REFRESH_JOB = snapshot
        threading.Thread(target=_refresh_worker, args=(timeout,), daemon=True).start()
        return True


def refresh_state() -> dict:
    """Снимок состояния проверки (копия — мутация результата безопасна).

    Поля: running (идёт ли проверка), started_at/done_at (time.time()),
    ok/count/errors — итог последней проверки refresh_models, checked_at —
    "HH:MM:SS" её завершения. До первой проверки — дефолт (все None/False)."""
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
