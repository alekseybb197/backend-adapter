"""Configuration: env var reads, model mapping, multi-backend globals,
backend probe, YAML parser, SSL context, utility functions.

This is the heaviest module (root of the dependency DAG).
"""

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

# ==================== НАСТРОЙКИ ====================
BACKEND_BASE = os.environ.get("ADAPTER_BACKEND_BASE", "https://llm.service.example.com")
BACKEND_KEY = os.environ.get("ADAPTER_BACKEND_KEY", "")
PROXY_PORT = int(os.environ.get("ADAPTER_PROXY_PORT", "9999"))
ADAPTER_DEBUG = os.environ.get("ADAPTER_DEBUG_ENABLE", "1").lower() not in ("0", "false", "no", "")
ADAPTER_DEBUG_LOGFILE = os.environ.get("ADAPTER_DEBUG_LOGFILE", "")
ADAPTER_TRACE_LOGFILE = os.environ.get("ADAPTER_TRACE_LOGFILE", "")
ADAPTER_DETACH = os.environ.get("ADAPTER_DETACH_ENABLE", "0").lower() in ("1", "true", "yes")
ADAPTER_TIMEOUT = int(os.environ.get("ADAPTER_TIMEOUT", "300"))
ADAPTER_RETRY = int(os.environ.get("ADAPTER_RETRY_COUNT", "3"))
ADAPTER_SKILL_PATTERNS = os.environ.get("ADAPTER_SKILL_PATTERNS", "")
ADAPTER_DEBUG_TRIM = int(os.environ.get("ADAPTER_DEBUG_TRIM", "3000"))
# Логгирование результатов работы инструментов:
#   ADAPTER_DEBUG_TOOLS=1 — писать все результаты ([TOOL_RESULT]).
#   ADAPTER_DEBUG_TOOLS_ERROR=1 (по умолчанию) — писать ошибки инструментов ([TOOL_RESULT_ERROR]).
#   ADAPTER_DEBUG_TAGS_FULL — перечисление тегов через запятую, для которых
#   отключается обрезка (trim). Если тэг в списке — полный вывод без обрезки.
#   Пример: BODY,OPENAI_BODY,FETCH_RAW,TOOL_RESULT,TOOL_RESULT_ERROR,RESPONSE
ADAPTER_DEBUG_TOOLS = os.environ.get("ADAPTER_DEBUG_TOOLS", "0").lower() not in (
    "0",
    "false",
    "no",
    "",
)
ADAPTER_DEBUG_TOOLS_ERROR = os.environ.get("ADAPTER_DEBUG_TOOLS_ERROR", "1").lower() not in (
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
# фиксирован — ADAPTER_DEBUG_TAGS_OUT_ALL). Срабатывает только если
# ADAPTER_DEBUG_LOGFILE указывает на директорию. Пусто / 0 / false — выкл.
ADAPTER_DEBUG_TAGS_OUT = os.environ.get("ADAPTER_DEBUG_TAGS_OUT", "").lower() not in (
    "0",
    "false",
    "no",
    "",
)
# Полный фиксированный список частей протокола, для которых пишутся дампы.
ADAPTER_DEBUG_TAGS_OUT_ALL = "BODY,OPENAI_BODY,FETCH_RAW,TOOL_RESULT_ERROR,TOOL_RESULT,RESPONSE"
# Веб-интерфейс просмотра сессий (backend_adapter/session_viewer.py):
#   ADAPTER_WEBUI_ENABLE=1 — поднять локальный веб-сервер на 127.0.0.1.
#   Срабатывает только если ADAPTER_DEBUG_LOGFILE указывает на директорию
#   (там лежат *.parts папки сессий). Порт — ADAPTER_WEBUI_PORT.
ADAPTER_WEBUI_ENABLE = os.environ.get("ADAPTER_WEBUI_ENABLE", "0").lower() not in (
    "0",
    "false",
    "no",
    "",
)
ADAPTER_WEBUI_PORT = int(os.environ.get("ADAPTER_WEBUI_PORT", "8765"))
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

# ==================== MULTI-BACKEND CONFIG ====================
ADAPTER_BACKEND_CONFIG = os.environ.get("ADAPTER_BACKEND_CONFIG", "")
# True, если используется legacy-режим (один бэкенд через BACKEND_BASE / BACKEND_KEY).
_BACKEND_LEGACY = ADAPTER_BACKEND_CONFIG == ""

# Глобальные структуры multi-backend.
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
          - name: BBB
            base: https://llm.service.another.com
            key: ADAPTER_BACKEND_KEY_BBB

    Возвращает список dict: {name, base, key} или None при ошибке."""
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
        # Продолжение текущей записи: "    base: ..." или "    key: ..."
        if current is not None:
            m2 = re.match(r"^\s+(\w+):\s*(.+)$", line)
            if m2:
                key_name = m2.group(1)
                if key_name in ("name", "base", "key"):
                    current[key_name] = m2.group(2).strip().strip('"').strip("'")

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


def _fetch_models(base: str, key: str) -> list[dict]:
    """Запрашивает GET /v1/models у бэкенда, возвращает список dict.

    ``base`` — URL бэкенда, ``key`` — уже раскрытый токен (в т.ч. из
    ADAPTER_BACKEND_CONFIG). Извлечение из os.environ — в _parse_backend_yaml.
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
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT)
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


def _probe_models() -> list[dict]:
    """Обёртка: проба legacy-бэкенда из ``_fetch_models``.

    Это используется при запуске без ``ADAPTER_BACKEND_CONFIG`` —
    старое поведение сохранено. BACKEND_KEY — уже раскрытый токен
    (из os.environ при импорте модуля)."""
    models = _fetch_models(BACKEND_BASE, BACKEND_KEY)
    print(f"[INIT] Fetched {len(models)} models from backend {BACKEND_BASE.rstrip('/')}/v1/models")
    for m in models:
        print(f"  model={m.get('id')}, object={m.get('object')}, owned_by={m.get('owned_by')}")
    return models


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

    global _BACKENDS, _BACKEND_BY_NAME, _DEFAULT_BACKEND

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

    # 2) Обнаружить коллизии по id
    id_counter: dict[str, int] = {}
    for m, _ in all_models:
        mid = m.get("id", "")
        id_counter[mid] = id_counter.get(mid, 0) + 1
    colliding_ids = {k for k, v in id_counter.items() if v > 1}

    # 3) Заполнить _AVAILABLE_MODELS и _MODEL_TO_BACKEND
    for m, backend in all_models:
        mid = m.get("id", "")
        if mid in colliding_ids:
            prefixed = f"{backend['name']}.{mid}"
            m["id"] = prefixed
            _AVAILABLE_MODELS[prefixed] = m
            _MODEL_TO_BACKEND[prefixed] = (backend["name"], backend)
        else:
            _AVAILABLE_MODELS[mid] = m
            _MODEL_TO_BACKEND[mid] = (backend["name"], backend)

    print(f"[INIT] Loaded {len(_AVAILABLE_MODELS)} models from {len(blocks)} backends")
    for mid in sorted(_AVAILABLE_MODELS.keys()):
        print(f"  model={mid}")


# ==================== MULTI-BACKEND: ROUTING ====================
def _resolve_backend(model: str) -> tuple[dict, str]:
    """Определить целевой бэкенд для модели.

    Возвращает ``(backend_cfg, resolved_model_name)``.

    Логика:
    1. Legacy-режим → единственный бэкенд, model без изменений.
    2. Явный префикс ``<backend_name>.<model>`` → stripping, routing.
    3. Lookup в ``_MODEL_TO_BACKEND`` → первый найденный бэкенд.
    4. Fallback → ``_DEFAULT_BACKEND``.
    """
    if _BACKEND_LEGACY:
        return {"base": BACKEND_BASE, "key": BACKEND_KEY, "name": "legacy"}, model

    # 2) Явный префикс: модель начинается с имени одного из бэкендов + '.'
    for bname, bcfg in _BACKEND_BY_NAME.items():
        prefix = bname + "."
        if model.startswith(prefix):
            actual = model[len(prefix) :]
            return bcfg, actual

    # 3) Lookup по известному списку
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

    # 4) Fallback
    if _DEFAULT_BACKEND:
        return _DEFAULT_BACKEND, model
    # Должно быть недостижимо — если multi-backend включён, но ни один
    # бэкенд не прошёл проб — это fatal (см. startup). На случай
    # неожиданных путей возвращаем legacy-конфиг.
    return {"base": BACKEND_BASE, "key": BACKEND_KEY, "name": "legacy"}, model
