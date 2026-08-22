#!/usr/bin/env python3
"""
Claude Code <-> OpenAI-backend adapter v6 (env-based config)
Fix: timeout + retry, ALL system messages at the beginning
Supports: tools, tool_choice, tool_use, tool_result, tool_calls fallback

Конфигурация:
  ADAPTER_BACKEND_BASE   — URL бэкенда
  ADAPTER_BACKEND_KEY    — API-ключ бэкенда
  ADAPTER_PROXY_PORT     — порт прокси (по умолч. 9999)
  ADAPTER_DEBUG_ENABLE   — логирование (1/true=yes)
  ADAPTER_DEBUG_LOGFILE  — файл для логов
  ADAPTER_DETACH_ENABLE  — фоновый режим (1/true=yes)
  ADAPTER_TIMEOUT        — таймаут к бэкенду в сек (по умолч. 300)
  ADAPTER_RETRY_COUNT    — число retry (по умолч. 3)
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

# ==================== НАСТРОЙКИ ====================
BACKEND_BASE      = os.environ.get("ADAPTER_BACKEND_BASE", "https://llm.service.example.com")
BACKEND_KEY       = os.environ.get("ADAPTER_BACKEND_KEY", "")
PROXY_PORT        = int(os.environ.get("ADAPTER_PROXY_PORT", "9999"))
ADAPTER_DEBUG     = os.environ.get("ADAPTER_DEBUG_ENABLE", "1").lower() not in ("0", "false", "no", "")
ADAPTER_DEBUG_LOGFILE = os.environ.get("ADAPTER_DEBUG_LOGFILE", "")
ADAPTER_DETACH    = os.environ.get("ADAPTER_DETACH_ENABLE", "0").lower() in ("1", "true", "yes")
ADAPTER_TIMEOUT   = int(os.environ.get("ADAPTER_TIMEOUT", "300"))
ADAPTER_RETRY     = int(os.environ.get("ADAPTER_RETRY_COUNT", "3"))
# ===================================================

def _d(msg: str) -> None:
    """Вывод лога: в консоль (если ADAPTER_DEBUG) или в файл (если задан ADAPTER_DEBUG_LOGFILE)."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}"
    if ADAPTER_DEBUG_LOGFILE:
        # Логируем в файл
        with open(ADAPTER_DEBUG_LOGFILE, "a") as f:
            f.write(line + "\n")
    elif ADAPTER_DEBUG:
        # Логируем в консоль
        print(line)

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


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

    # 1. System из отдельного поля Anthropic
    if system:
        if isinstance(system, str):
            system_parts.append(system)
        elif isinstance(system, list):
            for block in system:
                if block.get("type") == "text":
                    system_parts.append(block.get("text", ""))

    # 2. Проходим по messages: system -> в system_parts, остальное -> в other_msgs
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

    # Собираем результат: ОДИН system message в начале, потом всё остальное
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


def convert_openai_to_anthropic(o, model):
    """OpenAI response -> Anthropic response."""
    choice = o.get("choices", [{}])[0]
    message = choice.get("message", {}) or {}
    text = message.get("content") or ""
    tool_calls = message.get("tool_calls", [])
    finish_reason = choice.get("finish_reason", "stop")
    usage = o.get("usage", {})

    content = []
    if not tool_calls and text:
        parsed = parse_tool_calls_from_text(text)
        if parsed:
            tool_calls = parsed
            clean_text = re.sub(r'<tool_call>\s*\{.*?\}\s*</tool_call>', '', text, flags=re.DOTALL).strip()
            text = clean_text if clean_text else ""

    if text and text.strip():
        content.append({"type": "text", "text": text})

    for tc in tool_calls:
        if tc.get("type") == "function":
            func = tc.get("function", {})
            try:
                input_data = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                input_data = {}
            content.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "input": input_data
            })

    if not content:
        content = [{"type": "text", "text": " "}]

    stop_reason = finish_reason
    if finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif finish_reason not in ("stop", "length"):
        stop_reason = "end_turn"

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
        _d(f"\n{'='*70}")
        _d(f"[REQ] {self.command} {self.path}")
        for k, v in self.headers.items():
            _d(f"  {k}: {v}")

        if not self.path.startswith("/v1/messages"):
            self._send_json(404, {"error": "Expected /v1/messages"})
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        _d(f"[BODY] {body.decode()[:3000]}")

        try:
            anthropic_req = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        model = anthropic_req.get("model", "qwen3.6-35b-a3b")
        max_tokens = anthropic_req.get("max_tokens", 4096)

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
        if msgs and msgs[0]["role"] == "system":
            _d(f"[CHECK] First message is system, OK")
        else:
            _d(f"[WARN] First message is NOT system: {msgs[0]['role'] if msgs else 'empty'}")

        _d(f"[OPENAI_BODY] {json.dumps(openai_body, ensure_ascii=False)[:2500]}")

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
                t0 = time.time()
                resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT)
                raw = resp.read()
                elapsed = time.time() - t0
                _d(f"[FETCH] Success in {elapsed:.1f}s, {resp.status}, {len(raw)} bytes")
                _d(f"[FETCH_RAW] {raw.decode()[:3000]}")

                o = json.loads(raw)
                anthropic_resp = convert_openai_to_anthropic(o, model)

                _d(f"[RESPONSE] {json.dumps(anthropic_resp, ensure_ascii=False)[:800]}")
                self._send_json(200, anthropic_resp)
                _d("[OK] Done")
                return  # Успех — выходим

            except urllib.error.HTTPError as e:
                err = e.read().decode()
                _d(f"[BACKEND_ERR] HTTP {e.code} on attempt {attempt}: {err[:1500]}")
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
                last_error = ("timeout", str(e))
                if attempt < ADAPTER_RETRY:
                    delay = 2 ** attempt
                    _d(f"[RETRY] Waiting {delay}s before next attempt...")
                    time.sleep(delay)

            except Exception as e:
                _d(f"[FETCH_ERR] Attempt {attempt}/{ADAPTER_RETRY}: {type(e).__name__}: {e}")
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
            elif isinstance(code, int):
                _d(f"[FAIL] Backend returned HTTP {code}. Returning {code}.")
                self._send_json(code, {"error": f"Backend error: {msg}"})
            else:
                _d(f"[FAIL] Returning 502 after {ADAPTER_RETRY} attempts.")
                self._send_json(502, {"error": f"Backend unavailable after {ADAPTER_RETRY} attempts: {msg}"})


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
    print(f"Claude Code Adapter v6 (timeout+retry)")
    print(f"Listening:  http://localhost:{PROXY_PORT}")
    print(f"Backend:    {BACKEND_BASE}/v1/chat/completions")
    print(f"Timeout:    {ADAPTER_TIMEOUT}s")
    print(f"Retries:    {ADAPTER_RETRY}")
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
