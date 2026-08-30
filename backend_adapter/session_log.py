"""Session-scoped log file management.

Manages per-session debug/trace file handles with FIFO eviction.
Reads log path settings from config at import time.
"""
import json
import os
import re
import threading
import time
from collections import OrderedDict

_LOG_FILES_PER_SESSION = 5000
_session_logs: OrderedDict[str, dict] = OrderedDict()  # session_id -> {abspath: file_handle}
_session_file_ts: dict[str, str] = {}  # session_id -> stable timestamp (first-use)
_last_log_session_id: str = ""  # глобальный fallback для _d()

# Глобальный кэш путей к .parts директориям для разных сессий.
# Ключ: session_id или f"{session_id}_jsonparts" (для BODY_TAGS)
# Значение: путь к директории на диске.
_parts_dir: dict[str, str] = {}
_parts_dir_ts: dict[str, str] = {}  # session_id → stable timestamp (first-use)

# ==================== ADAPTER_DEBUG_BODY_TAGS ====================
# Глобальный счётчик для JSON-файлов тегированных блоков.
# Формат имени: <session[:8]>-<seq:04d>-<tag_lower>.json
_debug_json_seq = 0
_debug_json_lock = threading.Lock()


def _resolve_log_base():
    """Прочитать env-переменные и вернуть (debug_is_dir, debug_path,
    trace_is_dir, trace_path)."""
    dbg = os.environ.get("ADAPTER_DEBUG_LOGFILE", "")
    trc = os.environ.get("ADAPTER_TRACE_LOGFILE", "")
    return (
        os.path.isdir(dbg) if dbg else False, dbg,
        os.path.isdir(trc) if trc else False, trc,
    )


_DEBUG_IS_DIR, _DEBUG_PATH, _TRACE_IS_DIR, _TRACE_PATH = _resolve_log_base()


def _make_session_file(base_path: str, session_id: str, ext: str):
    """Возвращает путь к файлу сессии (вызывается только при is_dir=True).
    Формат имени: session-<YYYYMMDD-HHMMSS>-<sessionID>.<ext>.
    Timestamp фиксируется при первом обращении к сессии и переиспользуется,
    чтобы весь трафик сессии шёл в один файл."""
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', session_id[:8])
    if session_id not in _session_file_ts:
        _session_file_ts[session_id] = time.strftime("%Y%m%d-%H%M%S")
    ts = _session_file_ts[session_id]
    return os.path.join(base_path, f"session-{ts}-{safe}.{ext}")


def _open_session_file(kind: str, session_id: str):
    """Открыть дескриптор для session_id (создать или вернуть существующий).
    Returns open file handle (binary, "ab") или None, если режим «файл»."""
    if kind == "debug":
        is_dir, path = _DEBUG_IS_DIR, _DEBUG_PATH
        ext = "log"
    else:
        is_dir, path = _TRACE_IS_DIR, _TRACE_PATH
        ext = "jsonl"
    if not is_dir or not path:
        return None
    target = _make_session_file(path, session_id, ext)
    logs = _session_logs.setdefault(session_id, {})
    if target in logs:
        return logs[target]
    # Превысили лимит? Вытесняем самые старые сессии.
    while len(logs) > _LOG_FILES_PER_SESSION:
        _session_logs.popitem(last=False)
    fd = open(target, "ab")
    logs[target] = fd
    return fd


def _close_session_file(session_id: str) -> None:
    """Закрыть все открытые дескрипторы сессии (для чистоты при завершении)."""
    logs = _session_logs.pop(session_id, None)
    if logs:
        for f in logs.values():
            try:
                f.close()
            except Exception:
                pass


# ==================== ADAPTER_DEBUG_BODY_TAGS ====================


def _body_tags_parts_dir(session_id: str) -> str | None:
    """Возвращает путь к session-<YYYYMMDD-HHMMSS>-<session_id[:8]>.parts директории
    для сессии. Создаёт при первом вызове. Возвращает None, если per-session режим
    логов не включён.

    Формат директории: session-<YYYYMMDD-HHMMSS>-<sessionID[:8]>.parts
    Формат файлов: session-<YYYYMMDD-HHMMSS>-<sessionID[:8]>-NNNN-tagname.json
    """
    if not _DEBUG_IS_DIR or not _DEBUG_PATH:
        return None
    # Кэш по session_id
    tag_key = f"{session_id}_jsonparts"
    if tag_key not in _parts_dir:
        safe8 = session_id[:8]
        if session_id not in _parts_dir_ts:
            _parts_dir_ts[session_id] = time.strftime("%Y%m%d-%H%M%S")
        ts = _parts_dir_ts[session_id]
        tags_path = os.path.join(_DEBUG_PATH, f"session-{ts}-{safe8}.parts")
        os.makedirs(tags_path, exist_ok=True)
        _parts_dir[tag_key] = tags_path
    return _parts_dir[tag_key]


def write_debug_json(session_id: str, tag: str, data: dict | str) -> None:
    """Записать часть протокола обмена в JSON-файл.

    Формат имени: ``<session_id[:8]>-NNNN-<tag_lower>.json``

    ``data`` — dict, str, list или bytearray/bytes. При bytes/bytearray
    декодируется как UTF-8.

    Файл пишется только если включён per-session режим логов
    (``_DEBUG_IS_DIR``) и ``session_log.ADAPTER_DEBUG_BODY_TAGS``
    содержит данный ``tag``.
    """
    # Быстрая проверка — тег не в списке
    from .config import ADAPTER_DEBUG_BODY_TAGS
    if not ADAPTER_DEBUG_BODY_TAGS:
        return
    if tag not in ADAPTER_DEBUG_BODY_TAGS:
        return

    parts_path = _body_tags_parts_dir(session_id)
    if parts_path is None:
        return

    # Глобальный атомарный инкремент
    with _debug_json_lock:
        global _debug_json_seq
        _debug_json_seq += 1
        seq = _debug_json_seq
    tag_lower = tag.lower()
    json_name = f"{session_id[:8]}-{seq:04d}-{tag_lower}.json"
    json_path = os.path.join(parts_path, json_name)

    # Нормализация данных: bytes/bytearray → str, dict/list → json
    if isinstance(data, (bytes, bytearray)):
        body_str = data.decode("utf-8", errors="replace")
    elif isinstance(data, dict):
        body_str = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        body_str = str(data)

    with open(json_path, "w", encoding="utf-8") as f:
        f.write(body_str)
