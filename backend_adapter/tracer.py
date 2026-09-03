"""Structured trace logging (JSONL) and tool-use causality tracking.

Per-session JSONL trace with monotonically increasing sequence numbers,
and a bidirectional tool_use_id → req_id mapping for parent/child causality
in parallel agent turns.
"""

import json
import threading
import time
from collections import OrderedDict

from . import redact, session_log
from .config import ADAPTER_SENSITIVE_LOGGING_ENABLE

_trace_lock = threading.Lock()
_session_seq: dict[str, int] = {}  # session_id -> next seq number


def _next_seq(session_id: str) -> int:
    with _trace_lock:
        n = _session_seq.get(session_id, 0)
        _session_seq[session_id] = n + 1
        return n


# ==================== TOOL-USE ПРИЧИННОСТЬ (родитель -> потомок) ====================
# Один агентный цикл в рамках одной X-Claude-Code-Session-Id порождает много
# отдельных HTTP-запросов к адаптеру (req_id), причём часть из них может идти
# параллельно (см. request_kind: "structured_output" сайдкар для заголовка
# сессии обычно летит одновременно с основным "agent_turn"). Обычная
# сортировка по времени/msg_count для восстановления связки
# "какой запрос породил tool_use, а какой принёс его tool_result" ненадёжна
# именно в параллельном случае.
#
# Вместо этого используем tool_use_id как естественный, гарантированно
# уникальный ключ: он присваивается моделью/адаптером один раз на конкретный
# вызов инструмента (см. convert_openai_to_anthropic) и возвращается харнессом
# обратно в следующем запросе внутри tool_result-блока (см.
# convert_messages_anthropic_to_openai). Отображение "кто породил"
# храним per-session, с ограничением размера (на случай очень долгих
# сессий) — вытесняем самые старые записи по FIFO.
_TOOL_USE_INDEX_MAX_PER_SESSION = 2000
_tool_use_producers: dict[
    str, OrderedDict[str, str]
] = {}  # session_id -> OrderedDict(tool_use_id -> req_id)
_tool_use_names: dict[str, dict[str, str]] = {}  # session_id -> dict(tool_use_id -> tool_name)


def _register_tool_use(session_id: str, tool_use_id: str, req_id: str, tool_name: str = "") -> None:
    if not tool_use_id:
        return
    with _trace_lock:
        idx = _tool_use_producers.setdefault(session_id, OrderedDict())
        idx[tool_use_id] = req_id
        while len(idx) > _TOOL_USE_INDEX_MAX_PER_SESSION:
            idx.popitem(last=False)
        # Храним имя инструмента для отладки tool_result ошибок
        names = _tool_use_names.setdefault(session_id, {})
        names[tool_use_id] = tool_name


def _lookup_tool_use_producer(session_id: str, tool_use_id: str):
    with _trace_lock:
        idx = _tool_use_producers.get(session_id)
        if not idx:
            return None
        return idx.get(tool_use_id)


def _lookup_tool_use_name(session_id: str, tool_use_id: str):
    """Возвращает имя инструмента по tool_use_id или None."""
    with _trace_lock:
        names = _tool_use_names.get(session_id)
        if not names:
            return None
        return names.get(tool_use_id)


def _trace(session_id: str, req_id: str, event: str, **fields) -> None:
    # Если база — директория, пишем в сессионный файл; если полный путь —
    # в один файл (старое поведение).
    if session_log._TRACE_IS_DIR and session_log._TRACE_PATH:
        fd = session_log._open_session_file("trace", session_id)
        if not fd:
            return
    elif not session_log._TRACE_IS_DIR and not session_log._TRACE_PATH:
        return
    else:
        fd = None  # режим «один файл» — ниже open() на _TRACE_PATH

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}Z",
        "session_id": session_id,
        "req_id": req_id,
        "seq": _next_seq(session_id),
        "event": event,
    }
    record.update(fields)
    line = json.dumps(record, ensure_ascii=False, default=str)
    if not ADAPTER_SENSITIVE_LOGGING_ENABLE:
        line = redact.redact(line)
    with _trace_lock:
        if fd:
            fd.write((line + "\n").encode())
            fd.flush()
        else:
            with open(session_log._TRACE_PATH, "a") as f:
                f.write(line + "\n")
                f.flush()
