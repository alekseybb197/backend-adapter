"""Human-readable per-session debug logging (_d, _dr).

Calls config redaction, writes to console and/or per-session file in the
ADAPTER_DEBUG_LOGPATH directory (see session_log).
"""

import time

from . import config, session_log
from .redact import redact


def _write(msg: str) -> None:
    """Запись ПОЛНОЙ строки debug-лога в оба канала.

    Консоль — безусловна (печатается всегда) и пишет строку ОБРЕЗАННОЙ до
    ADAPTER_DEBUG_TRIM символов (0 = без обрезки): единственный
    всегда-включённый канал обязан уважать лимит консоли. Сессионный файл —
    только при ADAPTER_DEBUG_ENABLE=1 (config.ADAPTER_DEBUG) и получает
    ПОЛНУЮ строку без обрезки: файловый канал принципиально несёт части
    полностью (v0.8.6-реформа).

    При ADAPTER_SENSITIVE_LOGGING_ENABLE=1 санитайзер отключается —
    строка записывается без вызова redact(), т.е. полные токены, заголовки
    и ключи выводятся в открытом виде. По умолчанию санитайзер активен,
    секреты маскируются.

    ВАЖНО: config.ADAPTER_DEBUG / config.ADAPTER_SENSITIVE_LOGGING_ENABLE /
    trim_limit() читаются через МОДУЛЬНЫЙ атрибут (config.X), а не через
    `from .config import X` на уровне модуля — второе сделало бы разовый
    снимок значения при импорте, и переключение через /config API (см.
    webui_config_api.py) ничего бы не меняло здесь до перезапуска процесса."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    lim = config.trim_limit()
    body = msg[:lim] if lim else msg  # консольная обрезка (0/None — выкл.)
    if config.ADAPTER_SENSITIVE_LOGGING_ENABLE:
        console = f"[{ts}] {body}"
        full = f"[{ts}] {msg}"
    else:
        console = redact(f"[{ts}] {body}")
        full = redact(f"[{ts}] {msg}")
    print(console)
    # Файловая запись — только при мастер-флаге файловой записи
    if config.ADAPTER_DEBUG and session_log._DEBUG_IS_DIR and session_log._DEBUG_PATH:
        sid = session_log._last_log_session_id or "unknown"
        if sid == "unknown":
            return  # сессия ещё не установлена — не создаём пустой файл
        fd = session_log._open_session_file("debug", sid)
        if fd:
            fd.write((full + "\n").encode())
            fd.flush()


def _d(msg: str) -> None:
    """Вывод лога: в консоль ВСЕГДА (с обрезкой TRIM), в сессионный файл —
    полным при файловой записи (см. _write)."""
    _write(msg)


def _dr(req_id: str, msg: str) -> None:
    """Как _d(), но с префиксом [req_id] на каждой строке. Добавлено, чтобы
    при параллельных запросах (см. request_kind) строки разных req_id можно
    было различить в человекочитаемом логе, не сверяясь с JSON trace —
    раньше строки заголовков/тела разных одновременных запросов
    перемежались без какой-либо метки принадлежности."""
    _d(f"[{req_id}] {msg}")
