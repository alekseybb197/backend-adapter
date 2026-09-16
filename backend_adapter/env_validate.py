"""Строгая валидация env-переменных адаптера (v0.9.6).

Зачем: ``config.py`` читает env на уровне модуля, и часть переменных
парсится как ``int(...)`` — невалидное значение (``ADAPTER_PROXY_PORT=abc``)
роняло старт голым ``ValueError`` с traceback, без внятного сообщения о том,
что именно не так. Bool-переменные парсились мягко: ``ADAPTER_DEBUG_ENABLE=off``
молча трактовалось как ВКЛЮЧЕНО (``"off"`` не входило в набор «выключено») —
опечатка в конфиге давала противоположное задуманному поведение.

Теперь на старте (до чтения config) прогоняется таблица ``_ENV_SPECS``:

- **int** — значение обязано парситься в целое (иначе FATAL);
- **bool** — значение обязано входить в домен ``_BOOL_TRUE``/``_BOOL_FALSE``
  (``1/0/true/false/yes/no/on/off`` и пусто — иначе FATAL; регистр не важен);
- **str** — свободный текст, не проверяется.

Невалидное значение → ``[FATAL] <имя>=<знач>: ожидается <тип>`` в консоль и
``sys.exit(1)`` — адаптер не стартует (решение пользователя: невалидная env —
это ошибка конфигурации, а не повод тихо работать на дефолтах; ср. FATAL на
LOGPATH-файл и пустой ADAPTER_BACKEND_CONFIG в backend-adapter.py).

``config.py`` при импорте продолжает читать env сам (обратная совместимость
тестов, которые импортируют его без запуска скрипта), но парсит bool/int
через ``parse_bool``/``parse_int`` этого модуля — единый домен значений, без
расхождения между валидатором и парсером. На невалидном входе эти функции
возвращают безопасный дефолт (не бросают): строгость — за ``validate_env``.

Модуль — лист DAG: импортирует только stdlib. Вызывается из
``backend-adapter.py`` ПЕРЕД импортом ``config`` (иначе ``int()`` в config
успел бы упасть раньше проверки).
"""

import os
import sys

# Домены bool-значений (регистр не важен; strip — пробелы вокруг значения
# прощаем, как это делает sh при экспорте). Пустая строка — «выключено»:
# так трактуется незаданная через env переменная в файлах окружения.
_BOOL_TRUE = ("1", "true", "yes", "on")
_BOOL_FALSE = ("0", "false", "no", "off", "")

# Таблица строгой валидации env: имя → "bool" | "int" | "str". Переменные
# вне таблицы не проверяются (в т.ч. ADAPTER_*_TARGET — их домен
# валидируется в config._parse_target мягко: [WARN] + none, v0.9.4).
_ENV_SPECS: dict[str, str] = {
    # --- int ---
    "ADAPTER_PROXY_PORT": "int",
    "ADAPTER_TIMEOUT": "int",
    "ADAPTER_RETRY_COUNT": "int",
    "ADAPTER_DEBUG_TRIM": "int",
    "ADAPTER_TRACE_REASONING_MAX_CHARS": "int",
    "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS": "int",
    "ADAPTER_WEBUI_PORT": "int",
    "ADAPTER_EXPORTER_PORT": "int",
    "ADAPTER_MODEL_USAGE_SAVE_INTERVAL": "int",
    "ADAPTER_SESSIONS_TABLE": "int",
    "ADAPTER_REASONING_MIN_TOKENS": "int",
    "ADAPTER_REASONING_RETRY": "int",
    # --- bool ---
    "ADAPTER_DEBUG_ENABLE": "bool",
    "ADAPTER_DEBUG_PARTS": "bool",
    "ADAPTER_DETACH_ENABLE": "bool",
    "ADAPTER_STRICT_MODELS": "bool",
    "ADAPTER_STREAMING_ENABLE": "bool",
    "ADAPTER_STREAM_INCLUDE_USAGE": "bool",
    "ADAPTER_SENSITIVE_LOGGING_ENABLE": "bool",
    "ADAPTER_EXPORTER_ENABLE": "bool",
    "ADAPTER_MODEL_USAGE_ENABLE": "bool",
    # --- str (свободный текст) ---
    "ADAPTER_BACKEND_CONFIG": "str",
    "ADAPTER_ENDPOINT_HOST": "str",
    "ADAPTER_WEBUI_HOST": "str",
    "ADAPTER_DEBUG_LOGPATH": "str",
    "ADAPTER_PIDFILE": "str",
    "ADAPTER_STATE": "str",
    "ADAPTER_MODELS_MAPPING": "str",
    "ADAPTER_MODELS_TARIFFS": "str",
    "ADAPTER_SESSION_HEADER": "str",
}


def parse_bool(raw: str, default: bool = False) -> bool:
    """bool из env-значения; нераспознанное — ``default``.

    Домен — ``_BOOL_TRUE``/``_BOOL_FALSE`` (регистр и окружающие пробелы не
    важны). Пустая строка → False (как незаданная переменная). Невалидное
    значение возвращает ``default``, а не бросает: строгость обеспечивает
    ``validate_env`` на старте, а config.py при импорте остаётся мягким.
    """
    v = raw.strip().lower()
    if v in _BOOL_TRUE:
        return True
    if v in _BOOL_FALSE:
        return False
    return default


def parse_int(raw: str, default: int) -> int:
    """int из env-значения; нераспознанное — ``default``.

    Принимает только целое (``int(raw)``); ``abc``/``1.5``/пусто → default.
    """
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


def validate_env() -> None:
    """Проверить env по ``_ENV_SPECS``; при невалидном — FATAL и выход.

    Печатает ``[FATAL] <имя>=<знач>: ожидается <тип>`` и вызывает
    ``sys.exit(1)``. Незаданная переменная валидна всегда (дефолт берёт
    config). Вызывается из backend-adapter.py ДО импорта config.
    """
    for name, kind in _ENV_SPECS.items():
        raw = os.environ.get(name)
        if raw is None:
            continue
        if kind == "bool":
            if raw.strip().lower() not in _BOOL_TRUE + _BOOL_FALSE:
                _fatal(name, raw, "bool (1/0/true/false/yes/no/on/off)")
        elif kind == "int":
            try:
                int(raw.strip())
            except ValueError:
                _fatal(name, raw, "целое число (int)")


def _fatal(name: str, raw: str, expected: str) -> None:
    """Печать FATAL-сообщения и немедленный выход (адаптер не стартует)."""
    print(f"[FATAL] {name}={raw!r}: ожидается {expected}. Исправьте значение env.")
    sys.exit(1)
