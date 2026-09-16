"""JSON-файл результата опроса бэкенда на список моделей (v0.9.0).

Единственный вид файла — ``<имя_бэкенда>.models.json``: результат
``GET /v1/models`` по каждому бэкенду (каждый раз перезаписывается целиком).
Пишется в ``ADAPTER_DATA_ROOT/var`` (папка состояния) как плоский
JSON-файл — рядом с ``model-usage.yaml`` и PID-файлом; логи, трейсы и
``.err`` живут в соседней ``ADAPTER_DATA_ROOT/log``.

v0.9.9: дампы проб эндпойнтов (``<бэкенд>.<модель>.<эндпойнт>.json``)
удалены вместе с самими пробами — из активных проверок остался только
опрос списка моделей.

Канал записи БЕЗУСЛОВНЫЙ (как .err-файлы): пишется независимо от
ADAPTER_DEBUG_ENABLE / ADAPTER_DEBUG_PARTS / ADAPTER_DEBUG_TRIM; гейт —
только наличие папки состояния (путь всегда непуст: дефолт
``./tmp/adapter/var``).
Директория создаётся при записи (os.makedirs). Секреты маскируются по
умолчанию двумя слоями: структурно (значения ключей *_KEY/_TOKEN/_SECRET/
_PAT/API_KEY и key/token/secret/password/authorization — рекурсивно по
payload) и текстовым redact() поверх (Bearer-фрагменты, KEY=-обвязки в
error-строках); при ADAPTER_SENSITIVE_LOGGING_ENABLE=1 пишутся полные данные
(живое чтение config — как session_log.write_error_file). Запись никогда не
роняет проверку (все исключения глотаются) — канал наблюдательный.

Модуль — лист DAG: импортируется config.py (корень DAG), сам на верхнем
уровне импортирует только stdlib (config читается локально внутри функций
для живого SENSITIVE-флага — циклов импорта нет).
"""

import json
import os

# Ключи, чьи строковые значения считаются секретами и маскируются при
# дампе (case-insensitive): стандартные имена полей авторизации плюс
# суффиксы *_KEY/_TOKEN/_SECRET/_PAT/API_KEY (как паттерны redact.py).
# redact() на JSON-тексте такие пары не ловит — открывающая кавычка ключа
# ("token": "sk-...") не входит ни в имя-паттерн, ни в \s*: обходим саму
# структуру payload ДО сериализации, а redact() применяем к тексту поверх
# (Bearer-фрагменты и unquoted KEY=-обвязки внутри error-строк).
_SECRET_KEY_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PAT", "API_KEY")
_SECRET_KEY_EXACT = {"key", "token", "secret", "password", "authorization"}


def _is_secret_key(key: str) -> bool:
    k = key.strip().lower()
    if k in _SECRET_KEY_EXACT:
        return True
    return any(s in k for s in _SECRET_KEY_SUFFIXES)


def _mask_value(value: object) -> object:
    """Маска строкового значения в стиле redact._mask: короткие — целиком,
    длинные — с сохранением первых/последних 4 символов."""
    s = str(value)
    if len(s) <= 8:
        return "***REDACTED***"
    return f"{s[:4]}***REDACTED***{s[-4:]}"


def _mask_secrets(payload):
    """Рекурсивно замаскировать значения секретных ключей в структуре.

    ``payload`` — результат опроса (models-список бэкенда, error-строки).
    Строки заменяются маской в стиле redact._mask; вложенные dict/list
    обходятся рекурсивно. Ключи словаря НЕ трогаются — это имена моделей,
    не секреты. Bearer-фрагменты внутри error-строк (свободный текст, не
    пара «ключ: значение») добирает текстовый redact() в _write_json поверх
    сериализации."""
    if isinstance(payload, dict):
        return {
            key: (
                _mask_value(value)
                if _is_secret_key(key) and isinstance(value, str) and value
                else _mask_secrets(value)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_mask_secrets(item) for item in payload]
    return payload


def _logpath() -> str:
    """Папка состояния ``ADAPTER_DATA_ROOT/var`` без импорта config на верхнем
    уровне (та же формула, что у config.var_dir и daemon._pidfile_path):
    пусто/не задано → дефолт ``./tmp/adapter`` + ``var``."""
    root = os.environ.get("ADAPTER_DATA_ROOT", "").strip() or "./tmp/adapter"
    return os.path.join(root, "var")


def _write_json(file_name: str, payload: dict) -> None:
    """Атомарно перезаписать JSON-файл результата проверки.

    tmp + os.replace (как model_usage._atomic_write_yaml): «каждый раз
    перезаписывая» — файл целиком заменяется, .tmp-хвоста не остаётся.
    Провал записи (в т.ч. корень-файл вместо папки, права, отвал диска) молча
    глотается — канал наблюдательный, ронять проверку ему нельзя."""
    try:
        if not file_name:
            return
        from .config import ADAPTER_SENSITIVE_LOGGING_ENABLE
        from .redact import redact

        # Полные данные при SENSITIVE=1 (identity), иначе — маскирование
        # секретов: структурное (значения секретных ключей — _mask_secrets)
        # плюс redact() по тексту (Bearer-фрагменты и KEY=-обвязки в строках
        # ошибок; как write_error_file).
        if ADAPTER_SENSITIVE_LOGGING_ENABLE:
            text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
        else:
            masked = _mask_secrets(payload)
            text = json.dumps(masked, ensure_ascii=False, indent=2, sort_keys=False)
            text = redact(text)
        path = os.path.join(_logpath(), file_name)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        os.replace(tmp_path, path)
    except Exception:
        # Наблюдательный канал: любая ошибка записи молча глотается —
        # проверка бэкендов уже завершена к моменту вызова.
        pass


def write_models_json(backend_name: str, payload: dict) -> None:
    """Записать результат опроса бэкенда на доступные модели.

    Файл: ``<имя_бэкенда>.models.json`` в ``ADAPTER_DATA_ROOT/var``. ``payload`` —
    снимок результата ИМЕННО этого бэкенда: ``{"backend", "checked_at", "ok",
    "count", "models" | "error"}``. Вызывается из _init_multi_backends
    (старт), refresh_models (фоновая проверка/кнопка) и reload-перечитываний
    — каждый раз перезаписывает файл целиком."""
    _write_json(f"{backend_name}.models.json", payload)
