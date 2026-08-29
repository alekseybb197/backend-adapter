"""Session-scoped log file management.

Manages per-session debug/trace file handles with FIFO eviction.
Reads log path settings from config at import time.
"""
import os
import re
import time
from collections import OrderedDict

_LOG_FILES_PER_SESSION = 5000
_session_logs: OrderedDict[str, dict] = OrderedDict()  # session_id -> {abspath: file_handle}
_session_file_ts: dict[str, str] = {}  # session_id -> stable timestamp (first-use)
_last_log_session_id: str = ""  # глобальный fallback для _d()


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
