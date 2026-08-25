#!/usr/bin/env python3
"""Claude Code <-> OpenAI-backend adapter v0.3.2 (timeout+retry+trace+causality + per-session log files)
— changelog: ../changelog.md"""
import http.server
import socketserver
import urllib.request
import urllib.error
import json
import ssl
import re
import os
import sys
import time
import uuid
import threading
from collections import OrderedDict

# ==================== НАСТРОЙКИ ====================
BACKEND_BASE      = os.environ.get("ADAPTER_BACKEND_BASE", "https://llm.service.example.com")
BACKEND_KEY       = os.environ.get("ADAPTER_BACKEND_KEY", "")
PROXY_PORT        = int(os.environ.get("ADAPTER_PROXY_PORT", "9999"))
ADAPTER_DEBUG     = os.environ.get("ADAPTER_DEBUG_ENABLE", "1").lower() not in ("0", "false", "no", "")
ADAPTER_DEBUG_LOGFILE = os.environ.get("ADAPTER_DEBUG_LOGFILE", "")
ADAPTER_TRACE_LOGFILE = os.environ.get("ADAPTER_TRACE_LOGFILE", "")
ADAPTER_DETACH    = os.environ.get("ADAPTER_DETACH_ENABLE", "0").lower() in ("1", "true", "yes")
ADAPTER_TIMEOUT   = int(os.environ.get("ADAPTER_TIMEOUT", "300"))
ADAPTER_RETRY     = int(os.environ.get("ADAPTER_RETRY_COUNT", "3"))
ADAPTER_SKILL_PATTERNS = os.environ.get("ADAPTER_SKILL_PATTERNS", "")
ADAPTER_DEBUG_TRIM = int(os.environ.get("ADAPTER_DEBUG_TRIM", "3000"))
ADAPTER_DEBUG_BODY_FULL   = os.environ.get("ADAPTER_DEBUG_BODY_FULL", "0").lower() not in ("0", "false", "no", "")
ADAPTER_DEBUG_OPENAI_BODY_FULL   = os.environ.get("ADAPTER_DEBUG_OPENAI_BODY_FULL", "0").lower() not in ("0", "false", "no", "")
ADAPTER_DEBUG_FETCH_RAW_FULL = os.environ.get("ADAPTER_DEBUG_FETCH_RAW_FULL", "0").lower() not in ("0", "false", "no", "")
ADAPTER_TRACE_REASONING_MAX_CHARS = int(os.environ.get("ADAPTER_TRACE_REASONING_MAX_CHARS", "0"))
ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS = int(os.environ.get("ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS", "0"))
# ===================================================


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


# ==================== REDACTION ====================
# Секреты, которые нельзя писать в лог целиком ни при каких условиях:
# - значение Authorization: Bearer <...>
# - переменные вида *_PAT / *_KEY / *_TOKEN / *_SECRET = <значение>
# - длинные base64/hex-подобные строки (часто это и есть токены),
#   встреченные после характерных ключевых слов
_SECRET_PATTERNS = [
    (re.compile(r'(Bearer\s+)([A-Za-z0-9\-_\.=/+]{6,})', re.IGNORECASE),
     lambda m: m.group(1) + _mask(m.group(2))),
    (re.compile(r'((?:[A-Z0-9_]*(?:_PAT|_KEY|_TOKEN|_SECRET|API_KEY)[A-Z0-9_]*)\s*[:=]\s*["\']?)([A-Za-z0-9\-_\.\/+=]{8,})',
                re.IGNORECASE),
     lambda m: m.group(1) + _mask(m.group(2))),
]


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***REDACTED***"
    return f"{value[:4]}***REDACTED***{value[-4:]}"


def redact(text: str) -> str:
    """Применяет все паттерны редактирования секретов к произвольной строке."""
    if not text:
        return text
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() == "authorization":
            out[k] = redact(v)
        else:
            out[k] = v
    return out


# ==================== СЕССИОННЫЕ ЛОГ-ФАЙЫ ====================
# ADAPTER_DEBUG_LOGFILE / ADAPTER_TRACE_LOGFILE могут задаваться как:
#   1) Полный путь к файлу     — старое поведение, файл используется всегда
#   2) Путь к директории       — создаётся отдельный файл для каждой
#      сессии: session-<date_time>-<sessionID>.log (debug) или
#      session-<date_time>-<sessionID>.jsonl (trace). Файлы с одинаковым
#      session_id переоткрываются (append).
#
# Открываемые дескрипторы хранятся в _session_logs (session_id → handle),
# с защитой от бесконечного роста по FIFO.

_LOG_FILES_PER_SESSION = 5000
_session_logs: dict[str, dict] = {}  # session_id -> {abspath: file_handle}
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
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', session_id)
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


# ==================== ЧЕЛОВЕКОЧИТАЕМЫЙ ЛОГ ====================
def _d(msg: str) -> None:
    """Вывод лога: в консоль (если ADAPTER_DEBUG) и/или в файл (если задан
    ADAPTER_DEBUG_LOGFILE).
    Если ADAPTER_DEBUG_LOGFILE указывает на директорию — запись идёт в
    сессионный файл session-<sessionID>.log (см. _open_session_file).
    Если это полный путь — пишется в один файл (старое поведение)."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {redact(msg)}"
    if ADAPTER_DEBUG:
        print(line)
    # Писать в сессионный файл?
    if _DEBUG_IS_DIR and _DEBUG_PATH:
        sid = _last_log_session_id or "unknown"
        if sid == "unknown":
            return  # сессия ещё не установлена — не создаём пустой файл
        fd = _open_session_file("debug", sid)
        if fd:
            fd.write((line + "\n").encode())
    elif not _DEBUG_IS_DIR and _DEBUG_PATH:
        # Режим «один файл» (старое поведение)
        with open(_DEBUG_PATH, "a") as f:
            f.write(line + "\n")


def _dr(req_id: str, msg: str) -> None:
    """Как _d(), но с префиксом [req_id] на каждой строке. Добавлено, чтобы
    при параллельных запросах (см. request_kind) строки разных req_id можно
    было различить в человекочитаемом логе, не сверяясь с JSON trace —
    раньше строки заголовков/тела разных одновременных запросов
    перемежались без какой-либо метки принадлежности."""
    _d(f"[{req_id}] {msg}")


# ==================== СТРУКТУРИРОВАННЫЙ TRACE-ЛОГ (JSONL) ====================
# Один JSON-объект на строку. Поля, общие для всех событий:
#   ts        — ISO8601 с миллисекундами
#   session_id— из заголовка X-Claude-Code-Session-Id (или "unknown")
#   req_id    — уникальный id конкретного HTTP-запроса к адаптеру (не сессии)
#   seq       — монотонный счётчик событий ВНУТРИ сессии (не внутри запроса!),
#               позволяет восстановить полный таймлайн сессии из многих
#               последовательных запросов Claude Code
#   event     — тип события (см. ниже по коду)
_trace_lock = threading.Lock()
_session_seq = {}  # session_id -> next seq number


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
_tool_use_producers = {}  # session_id -> OrderedDict(tool_use_id -> req_id)


def _register_tool_use(session_id: str, tool_use_id: str, req_id: str) -> None:
    if not tool_use_id:
        return
    with _trace_lock:
        idx = _tool_use_producers.setdefault(session_id, OrderedDict())
        idx[tool_use_id] = req_id
        while len(idx) > _TOOL_USE_INDEX_MAX_PER_SESSION:
            idx.popitem(last=False)


def _lookup_tool_use_producer(session_id: str, tool_use_id: str):
    with _trace_lock:
        idx = _tool_use_producers.get(session_id)
        if not idx:
            return None
        return idx.get(tool_use_id)


def _trace(session_id: str, req_id: str, event: str, **fields) -> None:
    # Если база — директория, пишем в сессионный файл; если полный путь —
    # в один файл (старое поведение).
    if _TRACE_IS_DIR and _TRACE_PATH:
        fd = _open_session_file("trace", session_id)
        if not fd:
            return
    elif not _TRACE_IS_DIR and not _TRACE_PATH:
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
    line = redact(line)
    with _trace_lock:
        if fd:
            fd.write((line + "\n").encode())
        else:
            with open(_TRACE_PATH, "a") as f:
                f.write(line + "\n")


# ==================== SKILL DETECTION ====================
DEFAULT_SKILL_PATTERNS = {
    # skill_name -> список regex, которым может соответствовать
    # содержимое Bash-команды / путь к файлу в Read/Grep/Glob,
    # сигнализирующее об обращении к данному скиллу.
    "devtools":    [r'\.claude/skills/devtools', r'\.qwen/skills/devtools', r'chrome-devtools'],
    "frontmatter": [r'\.claude/skills/frontmatter', r'\.qwen/skills/frontmatter'],
    "klast":       [r'\.claude/skills/klast', r'\.qwen/skills/klast', r'\.klast/'],
    "mytasks":     [r'\.claude/skills/mytasks', r'\.qwen/skills/mytasks'],
    "prreview":    [r'\.claude/skills/prreview', r'\.qwen/skills/prreview'],
}


def _load_skill_patterns():
    if ADAPTER_SKILL_PATTERNS and os.path.isfile(ADAPTER_SKILL_PATTERNS):
        try:
            with open(ADAPTER_SKILL_PATTERNS) as f:
                raw = json.load(f)
            return {name: [re.compile(p, re.IGNORECASE) for p in pats] for name, pats in raw.items()}
        except Exception as e:
            _d(f"[SKILL_PATTERNS] Failed to load {ADAPTER_SKILL_PATTERNS}: {e}")
    return {name: [re.compile(p, re.IGNORECASE) for p in pats] for name, pats in DEFAULT_SKILL_PATTERNS.items()}


SKILL_PATTERNS = _load_skill_patterns()


def detect_skill(tool_name: str, tool_input: dict):
    """Пытается определить, к какому скиллу относится вызов инструмента.
    Возвращает (skill_name, evidence) или (None, None).
    Эвристика основана на путях/командах, а не на имени инструмента —
    харнесс обращается к скиллам через обычные Bash/Read/Grep/Glob,
    отдельного tool "Skill" в текущей связке не наблюдается (см. лог)."""
    haystack_parts = []
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path", "pattern", "notebook_path"):
            v = tool_input.get(key)
            if isinstance(v, str):
                haystack_parts.append(v)
    haystack = " ".join(haystack_parts)
    if not haystack:
        return None, None
    for skill_name, patterns in SKILL_PATTERNS.items():
        for pat in patterns:
            m = pat.search(haystack)
            if m:
                return skill_name, m.group(0)
    # Отдельно отмечаем именно чтение SKILL.md — это явный сигнал того,
    # что харнесс/модель обнаружила и загружает описание скилла, даже если
    # это скилл, не описанный в SKILL_PATTERNS (новый / незарегистрированный).
    if re.search(r'SKILL\.md', haystack, re.IGNORECASE):
        m = re.search(r'skills/([^/]+)/SKILL\.md', haystack, re.IGNORECASE)
        name = m.group(1) if m else "unknown"
        return f"unregistered:{name}", haystack
    return None, None


def extract_text(content):
    """Извлекает plain text из Anthropic content (строка или массив blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts)
    return str(content)


def convert_tools_anthropic_to_openai(tools):
    """Anthropic tool -> OpenAI tool."""
    openai_tools = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {})
            }
        })
    return openai_tools


def convert_tool_choice_anthropic_to_openai(tc):
    """Anthropic tool_choice -> OpenAI tool_choice."""
    if not tc:
        return "auto"
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "tool":
        return {"type": "function", "function": {"name": tc.get("name", "")}}
    return "auto"


def extract_tool_results(messages):
    """Достаёт все tool_result-блоки из входящих Anthropic messages "как есть",
    отдельно от convert_messages_anthropic_to_openai (которая тоже их видит,
    но для целей конвертации, а не трассировки). Возвращает список
    {tool_use_id, content, is_error}. Используется в do_POST, чтобы
    эмитить событие "tool_result" ПЕРЕД конвертацией в OpenAI-формат."""
    results = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_result":
                results.append({
                    "tool_use_id": block.get("tool_use_id", ""),
                    "content": extract_text(block.get("content")),
                    "is_error": bool(block.get("is_error", False)),
                })
    return results


def convert_messages_anthropic_to_openai(messages, system):
    """Конвертирует Anthropic messages + system в OpenAI messages.
    ВАЖНО: все system messages ДОЛЖНЫ быть в ОДНОМ сообщении в начале."""
    system_parts = []
    other_msgs = []

    if system:
        if isinstance(system, str):
            system_parts.append(system)
        elif isinstance(system, list):
            for block in system:
                if block.get("type") == "text":
                    system_parts.append(block.get("text", ""))

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            system_parts.append(extract_text(content))

        elif role == "user":
            if isinstance(content, list):
                text_parts = []
                tool_results = []
                for block in content:
                    if block.get("type") == "tool_result":
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": extract_text(block.get("content"))
                        })
                    elif block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                if text_parts:
                    other_msgs.append({"role": "user", "content": "\n".join(text_parts)})
                other_msgs.extend(tool_results)
            else:
                other_msgs.append({"role": "user", "content": extract_text(content)})

        elif role == "assistant":
            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {}))
                            }
                        })
                assistant_msg = {"role": "assistant"}
                if text_parts:
                    assistant_msg["content"] = "\n".join(text_parts)
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                if len(assistant_msg) > 1:
                    other_msgs.append(assistant_msg)
            else:
                other_msgs.append({"role": "assistant", "content": extract_text(content)})

    result = []
    if system_parts:
        result.append({"role": "system", "content": "\n".join(system_parts)})
    result.extend(other_msgs)
    return result


def parse_tool_calls_from_text(text):
    """Fallback: парсит <tool_call>...</tool_call> из текста (Qwen-формат)."""
    tool_calls = []
    pattern = r'<tool_call>\s*(\{.*?\})\s*</tool_call>'
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        try:
            data = json.loads(match)
            name = data.get("name") or data.get("function", {}).get("name")
            args = data.get("arguments") or data.get("function", {}).get("arguments") or data.get("parameters", {})
            if isinstance(args, str):
                args = json.loads(args)
            if name:
                tool_calls.append({
                    "id": f"call_{abs(hash(match)) % 10000000000}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}
                })
        except Exception:
            continue

    if not tool_calls and text.strip().startswith("{"):
        try:
            data = json.loads(text.strip())
            name = data.get("name")
            args = data.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            if name:
                tool_calls.append({
                    "id": f"call_{abs(hash(text)) % 10000000000}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}
                })
        except Exception:
            pass
    return tool_calls


def convert_openai_to_anthropic(o, model, session_id="unknown", req_id="unknown"):
    """OpenAI response -> Anthropic response.
    Дополнительно (по сравнению с v6): извлекает reasoning_content и
    трассирует ветвления (fallback-парсинг, маппинг finish_reason,
    skill-детекцию по каждому tool_use)."""
    choice = o.get("choices", [{}])[0]
    message = choice.get("message", {}) or {}
    text = message.get("content") or ""
    tool_calls = message.get("tool_calls", [])
    finish_reason = choice.get("finish_reason", "stop")
    usage = o.get("usage", {})
    reasoning = message.get("reasoning_content", "") or ""

    used_text_fallback = False
    if not tool_calls and text:
        parsed = parse_tool_calls_from_text(text)
        if parsed:
            used_text_fallback = True
            tool_calls = parsed
            clean_text = re.sub(r'<tool_call>\s*\{.*?\}\s*</tool_call>', '', text, flags=re.DOTALL).strip()
            text = clean_text if clean_text else ""

    if used_text_fallback:
        # ВАЖНО для оценки качества следования скиллам: модель не смогла
        # (или харнесс не смог) использовать нативный tool-calling формат
        # backend'а и адаптеру пришлось парсить JSON из текста руками.
        # Это деградация, повышающая риск некорректного вызова инструмента
        # скилла (обрезанный JSON, лишний текст рядом и т.п.). Это реальное
        # ветвление поведения адаптера с последствиями — поэтому у него
        # собственное событие, а не общий "harness_branch".
        _trace(session_id, req_id, "tool_call_fallback",
               parsed_count=len(tool_calls),
               raw_text_len=len(text))

    content = []
    if text and text.strip():
        content.append({"type": "text", "text": text})

    tool_use_summaries = []
    for tc in tool_calls:
        if tc.get("type") == "function":
            func = tc.get("function", {})
            try:
                input_data = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                input_data = {}
            name = func.get("name", "")
            tool_use_id = tc.get("id", "")
            content.append({
                "type": "tool_use",
                "id": tool_use_id,
                "name": name,
                "input": input_data
            })
            # Регистрируем, что ИМЕННО ЭТОТ запрос (req_id) породил данный
            # tool_use_id — это и есть узел "родитель" для последующей
            # причинной связи, когда где-то в будущем запросе придёт
            # соответствующий tool_result (см. do_POST/extract_tool_results
            # и событие "tool_result" ниже).
            _register_tool_use(session_id, tool_use_id, req_id)
            skill, evidence = detect_skill(name, input_data)
            traced_input = input_data
            if ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS > 0:
                serialized = json.dumps(input_data, ensure_ascii=False, default=str)
                if len(serialized) > ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS:
                    traced_input = _cap(serialized, ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS)
            tool_use_summaries.append({
                "id": tool_use_id, "name": name, "skill": skill,
                # Полные аргументы вызова, не только имя — без них нельзя
                # отличить содержательно разные вызовы одного инструмента
                # (см. пример с двумя разными "ls" из параллельных веток).
                # Секреты вычищаются позже, при записи всей trace-строки
                # (см. _trace -> redact(line)).
                "input": traced_input,
            })
            if skill:
                _trace(session_id, req_id, "skill_signal",
                       tool_id=tool_use_id, tool_name=name,
                       skill=skill, evidence=evidence[:200])

    if not content:
        content = [{"type": "text", "text": " "}]

    stop_reason = finish_reason
    if finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif finish_reason not in ("stop", "length"):
        stop_reason = "end_turn"

    # Маппинг finish_reason -> stop_reason — детерминированная функция
    # одного значения в другое, а не решение/ветвление; раньше под неё
    # заводилось отдельное событие "harness_branch". Теперь это просто два
    # поля внутри response_content, где и так уже есть остальной результат
    # этого же ответа модели.
    _trace(session_id, req_id, "response_content",
           text_len=len(text), tool_uses=tool_use_summaries,
           finish_reason_raw=finish_reason, stop_reason_mapped=stop_reason,
           reasoning_present=bool(reasoning.strip()), reasoning_len=len(reasoning),
           # Полный reasoning, а не reasoning[:500] — обрезка убивала как
           # раз ту часть рассуждения, где объясняется выбор ветки/тула.
           reasoning=_cap(reasoning, ADAPTER_TRACE_REASONING_MAX_CHARS))

    return {
        "id": f"msg_{o.get('id', 'local')}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0)
        },
        "stop_reason": stop_reason
    }


class QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer, который не сыплет полным traceback в лог, если
    клиент (Claude Code) уже закрыл соединение раньше, чем адаптер успел
    ответить. Это перехватывает обрыв на уровне socketserver.handle_error,
    поэтому работает для ЛЮБОЙ точки записи в ответ — не только для нашего
    _send_json, но и, например, для встроенного BaseHTTPRequestHandler
    .send_error() (он вызывается для неподдерживаемых методов вроде HEAD,
    как в случае с 'HEAD /api/hello' в логе)."""

    def handle_error(self, request, client_address):
        exc_type, exc_value, _ = sys.exc_info()
        if isinstance(exc_value, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            _d(f"[CLIENT_GONE] {client_address}: {exc_type.__name__ if exc_type else '?'}: {exc_value}")
            return
        # Любая другая ошибка — стандартное поведение (полный traceback в лог)
        super().handle_error(request, client_address)


class Adapter(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        _d(f"[HTTP] {fmt % args}")

    def _send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            # Клиент (Claude Code) уже закрыл соединение — обычно это значит,
            # что он не дождался ответа (свой таймаут короче, чем наш
            # ADAPTER_TIMEOUT * ADAPTER_RETRY_COUNT + backoff). Это не ошибка
            # адаптера, поэтому просто логируем и выходим, не роняя процесс.
            _d(f"[CLIENT_GONE] {type(e).__name__} during sending status={status}: client disconnected before response could be sent")
        except Exception as e:
            _d(f"[WARN] Error sending response: {type(e).__name__}: {e}")

    def do_HEAD(self):
        # Кто-то (health-check / сетевой пробник) стучится HEAD-запросами на
        # произвольные пути вроде /api/hello. Базовый BaseHTTPRequestHandler
        # не умеет HEAD и отвечает 501 через send_error(), что и породило
        # второй BrokenPipeError в логе. Отвечаем простым 200 без тела —
        # этого достаточно для любого health-check'а.
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        req_t0 = time.time()
        session_id = self.headers.get("X-Claude-Code-Session-Id", "unknown")
        req_id = uuid.uuid4().hex[:12]

        # Обновляем глобальный fallback для _d(), чтобы человекочитаемый
        # лог тоже писался в правильный сессионный файл.
        global _last_log_session_id
        _last_log_session_id = session_id

        _d(f"\n{'='*70}")
        _dr(req_id, f"[REQ] {self.command} {self.path} session={session_id}")
        for k, v in redact_headers(self.headers).items():
            _dr(req_id, f"  {k}: {v}")

        if not self.path.startswith("/v1/messages"):
            self._send_json(404, {"error": "Expected /v1/messages"})
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        _body = body.decode()
        _dr(req_id, f"[BODY] {(_body if ADAPTER_DEBUG_BODY_FULL else _body[:ADAPTER_DEBUG_TRIM])}")

        try:
            anthropic_req = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        model = anthropic_req.get("model", "qwen3.6-35b-a3b")
        max_tokens = anthropic_req.get("max_tokens", 4096)
        in_tools = anthropic_req.get("tools", [])
        in_tool_names = [t.get("name", "?") for t in in_tools]
        in_messages = anthropic_req.get("messages", [])
        has_structured_output = bool(
            (anthropic_req.get("output_config") or {}).get("format")
        )

        # request_kind — СТРУКТУРНЫЙ (не по содержимому промпта) признак
        # того, к какому потоку в рамках сессии относится запрос:
        #   agent_turn        — обычный ход основного агентного цикла
        #                        (есть хотя бы один tool)
        #   structured_output — сайдкар-вызов вроде генерации заголовка
        #                        сессии: 0 tools + output_config.format
        #                        задан. Идёт параллельно основному циклу,
        #                        НЕ является его веткой, хотя делит тот же
        #                        session_id.
        #   plain              — ни тулов, ни structured output (редкий
        #                        случай, например самый первый system-title
        #                        запрос без tools).
        if in_tools:
            request_kind = "agent_turn"
        elif has_structured_output:
            request_kind = "structured_output"
        else:
            request_kind = "plain"

        # Требовал ли исходный запрос стриминг — адаптер всегда шлёт
        # backend'у stream=False (см. openai_body ниже) независимо от этого
        # значения; раньше расхождение нигде не логировалось.
        _dr(req_id, f"[STREAM_REQUESTED] anthropic_stream={anthropic_req.get('stream')} -> backend_stream=False")

        _trace(session_id, req_id, "request_start",
               path=self.path, model=model, max_tokens=max_tokens,
               msg_count=len(in_messages),
               tool_count=len(in_tools), tool_names=in_tool_names,
               tool_choice=anthropic_req.get("tool_choice"),
               request_kind=request_kind,
               stream_requested=anthropic_req.get("stream"))

        # === Трассировка исполнения инструмента (tool_result) ===
        # Делается ДО конвертации в OpenAI-формат и ДО отправки бэкенду —
        # это чисто наблюдение за тем, что харнесс уже прислал нам в этом
        # запросе. Каждый tool_result связывается с req_id запроса, который
        # породил соответствующий tool_use (см. _register_tool_use в
        # convert_openai_to_anthropic), через уникальный tool_use_id.
        for tr in extract_tool_results(in_messages):
            parent_req_id = _lookup_tool_use_producer(session_id, tr["tool_use_id"])
            traced_content = tr["content"]
            if ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS > 0:
                traced_content = _cap(traced_content, ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS)
            _trace(session_id, req_id, "tool_result",
                   tool_use_id=tr["tool_use_id"],
                   # parent_req_id=None означает, что производитель этого
                   # tool_use не найден в индексе данной сессии — либо он
                   # был вытеснен по FIFO (см. _TOOL_USE_INDEX_MAX_PER_SESSION),
                   # либо tool_use случился до перезапуска адаптера/вне
                   # трассировки. Само по себе это не ошибка, но означает,
                   # что причинная связь для данного узла восстановить
                   # нельзя — только его содержимое.
                   parent_req_id=parent_req_id,
                   is_error=tr["is_error"],
                   content=traced_content)
            _dr(req_id, f"[TOOL_RESULT] tool_use_id={tr['tool_use_id']} parent_req_id={parent_req_id} is_error={tr['is_error']} len={len(tr['content'])}")

        openai_body = {
            "model": model,
            "messages": convert_messages_anthropic_to_openai(
                anthropic_req.get("messages", []),
                anthropic_req.get("system")
            ),
            "max_tokens": max_tokens,
            "stream": False
        }

        if "tools" in anthropic_req:
            openai_body["tools"] = convert_tools_anthropic_to_openai(anthropic_req["tools"])
            _dr(req_id, f"[TOOLS] Passed {len(openai_body['tools'])} tools")

        if "tool_choice" in anthropic_req:
            openai_body["tool_choice"] = convert_tool_choice_anthropic_to_openai(anthropic_req["tool_choice"])
            _dr(req_id, f"[TOOL_CHOICE] {openai_body['tool_choice']}")

        # Проверяем, что system действительно в начале. Это диагностика
        # ИНВАРИАНТА КОНВЕРТАЦИИ самого адаптера (Anthropic->OpenAI), а не
        # решение модели или харнесса — раньше это писалось под общим,
        # вводящим в заблуждение именем "harness_branch".
        msgs = openai_body["messages"]
        system_ok = bool(msgs) and msgs[0]["role"] == "system"
        _trace(session_id, req_id, "adapter_invariant_check",
               check="system_message_first", passed=system_ok,
               first_role=(msgs[0]["role"] if msgs else None))
        if system_ok:
            _dr(req_id, f"[CHECK] First message is system, OK")
        else:
            _dr(req_id, f"[WARN] First message is NOT system: {msgs[0]['role'] if msgs else 'empty'}")

        _dr(req_id, f"[OPENAI_BODY] {(json.dumps(openai_body, ensure_ascii=False) if ADAPTER_DEBUG_OPENAI_BODY_FULL else json.dumps(openai_body, ensure_ascii=False)[:ADAPTER_DEBUG_TRIM])}")

        backend_url = BACKEND_BASE.rstrip('/') + "/v1/chat/completions"
        req = urllib.request.Request(
            backend_url,
            data=json.dumps(openai_body, ensure_ascii=False).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {BACKEND_KEY}",
                "Connection": "keep-alive",
            },
            method="POST"
        )

        # === Retry loop ===
        last_error = None
        for attempt in range(1, ADAPTER_RETRY + 1):
            try:
                _dr(req_id, f"[FETCH] Attempt {attempt}/{ADAPTER_RETRY}, timeout={ADAPTER_TIMEOUT}s")
                _trace(session_id, req_id, "backend_attempt", attempt=attempt, timeout=ADAPTER_TIMEOUT)
                t0 = time.time()
                resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT)
                raw = resp.read()
                elapsed = time.time() - t0
                _dr(req_id, f"[FETCH] Success in {elapsed:.1f}s, {resp.status}, {len(raw)} bytes")
                _dr(req_id, f"[FETCH_RAW] {(raw.decode() if ADAPTER_DEBUG_FETCH_RAW_FULL else raw.decode()[:ADAPTER_DEBUG_TRIM])}")
                _trace(session_id, req_id, "backend_result", attempt=attempt,
                       ok=True, status=resp.status, elapsed_ms=int(elapsed * 1000))

                o = json.loads(raw)
                anthropic_resp = convert_openai_to_anthropic(o, model, session_id, req_id)

                _dr(req_id, f"[RESPONSE] {json.dumps(anthropic_resp, ensure_ascii=False)[:800]}")
                self._send_json(200, anthropic_resp)
                _dr(req_id, "[OK] Done")
                _trace(session_id, req_id, "request_end",
                       http_status=200, retries_used=attempt - 1,
                       total_elapsed_ms=int((time.time() - req_t0) * 1000))
                return  # Успех — выходим

            except urllib.error.HTTPError as e:
                err = e.read().decode()
                _dr(req_id, f"[BACKEND_ERR] HTTP {e.code} on attempt {attempt}: {err[:1500]}")
                _trace(session_id, req_id, "backend_result", attempt=attempt,
                       ok=False, status=e.code, error=redact(err[:500]))
                last_error = (e.code, err)
                # HTTP-ошибки (4xx) retry не делаем, кроме 429/503/504
                if e.code not in (429, 502, 503, 504):
                    break
                if attempt < ADAPTER_RETRY:
                    delay = 2 ** attempt
                    _dr(req_id, f"[RETRY] Waiting {delay}s...")
                    time.sleep(delay)

            except TimeoutError as e:
                _dr(req_id, f"[TIMEOUT] Attempt {attempt}/{ADAPTER_RETRY} timed out after {ADAPTER_TIMEOUT}s")
                _trace(session_id, req_id, "backend_result", attempt=attempt,
                       ok=False, status="timeout", error=str(e))
                last_error = ("timeout", str(e))
                if attempt < ADAPTER_RETRY:
                    delay = 2 ** attempt
                    _dr(req_id, f"[RETRY] Waiting {delay}s before next attempt...")
                    time.sleep(delay)

            except Exception as e:
                _dr(req_id, f"[FETCH_ERR] Attempt {attempt}/{ADAPTER_RETRY}: {type(e).__name__}: {e}")
                _trace(session_id, req_id, "backend_result", attempt=attempt,
                       ok=False, status="error", error=f"{type(e).__name__}: {e}")
                last_error = ("error", str(e))
                if attempt < ADAPTER_RETRY:
                    delay = 2 ** attempt
                    _dr(req_id, f"[RETRY] Waiting {delay}s...")
                    time.sleep(delay)

        # Все попытки исчерпаны
        if last_error:
            code, msg = last_error
            if code == "timeout":
                _dr(req_id, f"[FAIL] All {ADAPTER_RETRY} attempts timed out. Returning 504.")
                self._send_json(504, {"error": f"Gateway timeout after {ADAPTER_RETRY} attempts: {msg}"})
                final_status = 504
            elif isinstance(code, int):
                _dr(req_id, f"[FAIL] Backend returned HTTP {code}. Returning {code}.")
                self._send_json(code, {"error": f"Backend error: {msg}"})
                final_status = code
            else:
                _dr(req_id, f"[FAIL] Returning 502 after {ADAPTER_RETRY} attempts.")
                self._send_json(502, {"error": f"Backend unavailable after {ADAPTER_RETRY} attempts: {msg}"})
                final_status = 502
            _trace(session_id, req_id, "request_end",
                   http_status=final_status, retries_used=ADAPTER_RETRY,
                   total_elapsed_ms=int((time.time() - req_t0) * 1000), failed=True)


def _detach() -> None:
    """Отпустить процесс от консоли: двойной fork + отключение stdio."""
    try:
        pid = os.fork()
        if pid > 0:
            # Родитель: выходим сразу
            time.sleep(0.5)
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"[FORK] Error: [{e.errno}] {e.strerror}\n")
        sys.exit(1)

    # Первый дочерний: создаём новую сессию (отвязываемся от терминала)
    os.setsid()

    try:
        pid = os.fork()
        if pid > 0:
            # Первый потомок завершается
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"[FORK] Error: [{e.errno}] {e.strerror}\n")
        sys.exit(1)

    # Второй потомок: перенаправляем stdio в/dev/null
    sys.stdout.flush()
    sys.stderr.flush()

    with open(os.devnull, "r") as fin:
        os.dup2(fin.fileno(), sys.stdin.fileno())
    with open(os.devnull, "w") as fout:
        os.dup2(fout.fileno(), sys.stdout.fileno())
        os.dup2(fout.fileno(), sys.stderr.fileno())


def _write_pidfile() -> None:
    """Записать PID процесса в файл pid."""
    pidfile = os.environ.get("ADAPTER_PIDFILE", "/tmp/adapter.pid")
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))


if __name__ == "__main__":
    if ADAPTER_DETACH:
        print(f"[DETACH] Starting as background service...")
        print(f"Backend:  {BACKEND_BASE}/v1/chat/completions")
        print(f"Timeout:  {ADAPTER_TIMEOUT}s")
        print(f"Retries:  {ADAPTER_RETRY}")
        _detach()
        _write_pidfile()

    print(f"\n{'='*70}")
    print(f"Claude Code Adapter v0.2.0 (timeout+retry+trace)")
    print(f"Listening:  http://localhost:{PROXY_PORT}")
    print(f"Backend:    {BACKEND_BASE}/v1/chat/completions")
    print(f"Timeout:    {ADAPTER_TIMEOUT}s")
    print(f"Retries:    {ADAPTER_RETRY}")
    print(f"Trace log:  {ADAPTER_TRACE_LOGFILE or '(disabled)'}")
    print(f"{'='*70}\n")
    # ThreadingHTTPServer вместо socketserver.TCPServer: Claude Code может
    # открывать несколько параллельных запросов (конкурентные tool calls),
    # а однопоточный сервер обрабатывает их строго последовательно — пока
    # первый запрос ждёт ADAPTER_TIMEOUT секунд от бэкенда, остальные
    # соединения простаивают в очереди accept() и клиент рвёт их по своему
    # таймауту. Это и есть основной источник BrokenPipeError в логе.
    Adapter.daemon_threads = True
    with QuietThreadingHTTPServer(("", PROXY_PORT), Adapter) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[EXIT] Bye")
