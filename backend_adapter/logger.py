"""Human-readable per-session debug logging (_d, _dr).

Calls config redaction, writes to console and/or session file (or single
file) depending on ADAPTER_DEBUG_LOGFILE setting.
"""

import os
import time

from . import session_log
from .config import ADAPTER_DEBUG, ADAPTER_SENSITIVE_LOGGING_ENABLE
from .redact import redact


def _d(msg: str) -> None:
    """Вывод лога: в консоль (если ADAPTER_DEBUG) и/или в файл (если задан
    ADAPTER_DEBUG_LOGFILE).
    Если ADAPTER_DEBUG_LOGFILE указывает на директорию — запись идёт в
    сессионный файл session-<sessionID>.log (см. _open_session_file).
    Если это полный путь — пишется в один файл (старое поведение).

    При ADAPTER_SENSITIVE_LOGGING_ENABLE=1 санитайзер отключается —
    строка записывается в лог без вызова redact(), т.е. полные токены,
    заголовки и ключи выводятся в открытом виде. По умолчанию санитайзер
    активен, секреты маскируются."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}" if ADAPTER_SENSITIVE_LOGGING_ENABLE else f"[{ts}] {redact(msg)}"
    if ADAPTER_DEBUG:
        print(line)
    # Писать в сессионный файл?
    if session_log._DEBUG_IS_DIR and session_log._DEBUG_PATH:
        sid = session_log._last_log_session_id or "unknown"
        if sid == "unknown":
            return  # сессия ещё не установлена — не создаём пустой файл
        fd = session_log._open_session_file("debug", sid)
        if fd:
            fd.write((line + "\n").encode())
            fd.flush()
    elif not session_log._DEBUG_IS_DIR and session_log._DEBUG_PATH:
        # Режим «один файл» (старое поведение)
        # Создаём директорию если её нет — иначе `open(".../nonexistent/foo.log",
        # "a")` падает с FileNotFoundError (директория должна существовать)
        parent = os.path.dirname(session_log._DEBUG_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(session_log._DEBUG_PATH, "a") as f:
            f.write(line + "\n")


def _dr(req_id: str, msg: str) -> None:
    """Как _d(), но с префиксом [req_id] на каждой строке. Добавлено, чтобы
    при параллельных запросах (см. request_kind) строки разных req_id можно
    было различить в человекочитаемом логе, не сверяясь с JSON trace —
    раньше строки заголовков/тела разных одновременных запросов
    перемежались без какой-либо метки принадлежности."""
    _d(f"[{req_id}] {msg}")
