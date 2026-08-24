#!/usr/bin/env python3
"""
Claude Code <-> OpenAI-backend adapter v0.2.0 (timeout+retry+trace)

Отличия от v6:
  - Добавлен структурированный JSONL trace-лог (ADAPTER_TRACE_LOGFILE),
    отдельный от человекочитаемого debug-лога. Одна строка = одно событие.
    Формат рассчитан на последующий парсинг trace_analyze.py.
  - Трассируются: какие инструменты харнесс предложил модели, что модель
    реально выбрала (tool_use), какие скиллы это затронуло (detect_skill),
    и внутренние ветвления самого адаптера (fallback-парсинг tool_call из
    текста, проверка позиции system-сообщения, маппинг finish_reason,
    retry-цепочка).
  - Из ответа бэкенда теперь извлекается reasoning_content (chain-of-thought
    провайдера, если он его отдаёт) — раньше он тихо терялся при конвертации
    в Anthropic-формат. Это единственное место, где видно, ПОЧЕМУ модель
    выбрала тот или иной инструмент/скилл, поэтому для целей трассировки
    он критичен, даже если наружу (клиенту Claude Code) он не идёт.
  - Секреты (Authorization, PAT/KEY/TOKEN-подобные значения) редактируются
    перед записью в ЛЮБОЙ лог — и debug, и trace. Это относится и к текущей
    сырой записи заголовков/тела, которая раньше писалась как есть.

Конфигурация (в дополнение к переменным v6):
  ADAPTER_TRACE_LOGFILE   — путь к JSONL trace-логу (если не задан — трассировка
                             событий harness/skill не пишется, но redaction
                             всё равно применяется к debug-логу)
  ADAPTER_SKILL_PATTERNS  — путь к JSON-файлу с описанием паттернов скиллов
                             (см. skill_patterns.json). Если не задан —
                             используется встроенный дефолт под текущий
                             CLAUDE.md (devtools, frontmatter, klast,
                             mytasks, prreview; и .claude/.qwen варианты).
"""
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
# ===================================================

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


# ==================== ЧЕЛОВЕКОЧИТАЕМЫЙ ЛОГ (как в v6, + redaction) ====================
def _d(msg: str) -> None:
    """Вывод лога: в консоль (если ADAPTER_DEBUG) и/или в файл (если задан ADAPTER_DEBUG_LOGFILE).
    Оба канала независимы — можно писать одновременно и в файл, и в консоль."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {redact(msg)}"
    if ADAPTER_DEBUG:
        print(line)
    if ADAPTER_DEBUG_LOGFILE:
        with open(ADAPTER_DEBUG_LOGFILE, "a") as f:
            f.write(line + "\n")


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


def _trace(session_id: str, req_id: str, event: str, **fields) -> None:
    if not ADAPTER_TRACE_LOGFILE:
        return
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
        with open(ADAPTER_TRACE_LOGFILE, "a") as f:
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
        # скилла (обрезанный JSON, лишний текст рядом и т.п.).
        _trace(session_id, req_id, "harness_branch",
               check="text_tool_call_fallback", triggered=True,
               parsed_count=len(tool_calls))

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
            content.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": name,
                "input": input_data
            })
            skill, evidence = detect_skill(name, input_data)
            tool_use_summaries.append({
                "id": tc.get("id", ""), "name": name, "skill": skill,
            })
            if skill:
                _trace(session_id, req_id, "skill_signal",
                       tool_id=tc.get("id", ""), tool_name=name,
                       skill=skill, evidence=evidence[:200])

    if not content:
        content = [{"type": "text", "text": " "}]

    stop_reason = finish_reason
    if finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif finish_reason not in ("stop", "length"):
        stop_reason = "end_turn"

    _trace(session_id, req_id, "harness_branch",
           check="finish_reason_map",
           openai_finish_reason=finish_reason, anthropic_stop_reason=stop_reason)

    _trace(session_id, req_id, "response_content",
           text_len=len(text), tool_uses=tool_use_summaries,
           reasoning_present=bool(reasoning.strip()), reasoning_len=len(reasoning),
           reasoning_preview=reasoning[:500])

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

        _d(f"\n{'='*70}")
        _d(f"[REQ] {self.command} {self.path}")
        for k, v in redact_headers(self.headers).items():
            _d(f"  {k}: {v}")

        if not self.path.startswith("/v1/messages"):
            self._send_json(404, {"error": "Expected /v1/messages"})
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        _body = body.decode()
        _d(f"[BODY] {(_body if ADAPTER_DEBUG_BODY_FULL else _body[:ADAPTER_DEBUG_TRIM])}")

        try:
            anthropic_req = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        model = anthropic_req.get("model", "qwen3.6-35b-a3b")
        max_tokens = anthropic_req.get("max_tokens", 4096)
        in_tools = anthropic_req.get("tools", [])
        in_tool_names = [t.get("name", "?") for t in in_tools]

        _trace(session_id, req_id, "request_start",
               path=self.path, model=model, max_tokens=max_tokens,
               msg_count=len(anthropic_req.get("messages", [])),
               tool_count=len(in_tools), tool_names=in_tool_names,
               tool_choice=anthropic_req.get("tool_choice"))

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
            _d(f"[TOOLS] Passed {len(openai_body['tools'])} tools")

        if "tool_choice" in anthropic_req:
            openai_body["tool_choice"] = convert_tool_choice_anthropic_to_openai(anthropic_req["tool_choice"])
            _d(f"[TOOL_CHOICE] {openai_body['tool_choice']}")

        # Проверяем, что system действительно в начале
        msgs = openai_body["messages"]
        system_ok = bool(msgs) and msgs[0]["role"] == "system"
        _trace(session_id, req_id, "harness_branch",
               check="system_first", passed=system_ok,
               first_role=(msgs[0]["role"] if msgs else None))
        if system_ok:
            _d(f"[CHECK] First message is system, OK")
        else:
            _d(f"[WARN] First message is NOT system: {msgs[0]['role'] if msgs else 'empty'}")

        _d(f"[OPENAI_BODY] {(json.dumps(openai_body, ensure_ascii=False) if ADAPTER_DEBUG_OPENAI_BODY_FULL else json.dumps(openai_body, ensure_ascii=False)[:ADAPTER_DEBUG_TRIM])}")

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
                _d(f"[FETCH] Attempt {attempt}/{ADAPTER_RETRY}, timeout={ADAPTER_TIMEOUT}s")
                _trace(session_id, req_id, "backend_attempt", attempt=attempt, timeout=ADAPTER_TIMEOUT)
                t0 = time.time()
                resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT)
                raw = resp.read()
                elapsed = time.time() - t0
                _d(f"[FETCH] Success in {elapsed:.1f}s, {resp.status}, {len(raw)} bytes")
                _d(f"[FETCH_RAW] {(raw.decode() if ADAPTER_DEBUG_FETCH_RAW_FULL else raw.decode()[:ADAPTER_DEBUG_TRIM])}")
                _trace(session_id, req_id, "backend_result", attempt=attempt,
                       ok=True, status=resp.status, elapsed_ms=int(elapsed * 1000))

                o = json.loads(raw)
                anthropic_resp = convert_openai_to_anthropic(o, model, session_id, req_id)

                _d(f"[RESPONSE] {json.dumps(anthropic_resp, ensure_ascii=False)[:800]}")
                self._send_json(200, anthropic_resp)
                _d("[OK] Done")
                _trace(session_id, req_id, "request_end",
                       http_status=200, retries_used=attempt - 1,
                       total_elapsed_ms=int((time.time() - req_t0) * 1000))
                return  # Успех — выходим

            except urllib.error.HTTPError as e:
                err = e.read().decode()
                _d(f"[BACKEND_ERR] HTTP {e.code} on attempt {attempt}: {err[:1500]}")
                _trace(session_id, req_id, "backend_result", attempt=attempt,
                       ok=False, status=e.code, error=redact(err[:500]))
                last_error = (e.code, err)
                # HTTP-ошибки (4xx) retry не делаем, кроме 429/503/504
                if e.code not in (429, 502, 503, 504):
                    break
                if attempt < ADAPTER_RETRY:
                    delay = 2 ** attempt
                    _d(f"[RETRY] Waiting {delay}s...")
                    time.sleep(delay)

            except TimeoutError as e:
                _d(f"[TIMEOUT] Attempt {attempt}/{ADAPTER_RETRY} timed out after {ADAPTER_TIMEOUT}s")
                _trace(session_id, req_id, "backend_result", attempt=attempt,
                       ok=False, status="timeout", error=str(e))
                last_error = ("timeout", str(e))
                if attempt < ADAPTER_RETRY:
                    delay = 2 ** attempt
                    _d(f"[RETRY] Waiting {delay}s before next attempt...")
                    time.sleep(delay)

            except Exception as e:
                _d(f"[FETCH_ERR] Attempt {attempt}/{ADAPTER_RETRY}: {type(e).__name__}: {e}")
                _trace(session_id, req_id, "backend_result", attempt=attempt,
                       ok=False, status="error", error=f"{type(e).__name__}: {e}")
                last_error = ("error", str(e))
                if attempt < ADAPTER_RETRY:
                    delay = 2 ** attempt
                    _d(f"[RETRY] Waiting {delay}s...")
                    time.sleep(delay)

        # Все попытки исчерпаны
        if last_error:
            code, msg = last_error
            if code == "timeout":
                _d(f"[FAIL] All {ADAPTER_RETRY} attempts timed out. Returning 504.")
                self._send_json(504, {"error": f"Gateway timeout after {ADAPTER_RETRY} attempts: {msg}"})
                final_status = 504
            elif isinstance(code, int):
                _d(f"[FAIL] Backend returned HTTP {code}. Returning {code}.")
                self._send_json(code, {"error": f"Backend error: {msg}"})
                final_status = code
            else:
                _d(f"[FAIL] Returning 502 after {ADAPTER_RETRY} attempts.")
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
