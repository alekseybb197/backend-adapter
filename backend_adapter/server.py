"""HTTP server: Adapter handler, QuietThreadingHTTPServer, __main__.

Serves the Claude Code API (/v1/messages, /v1/models) and proxies
requests to the configured OpenAI-compatible backend(s).
"""

import contextlib
import http.server
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import config, model_usage, routing, session_log
from .config import (
    _AVAILABLE_MODELS,
    _MAP,
    ADAPTER_RETRY,
    ADAPTER_TIMEOUT,
    SSL_CTX,
    _cap,
    _resolve_backend,
)
from .convert import (
    convert_messages_anthropic_to_openai,
    convert_openai_to_anthropic,
    convert_tool_choice_anthropic_to_openai,
    convert_tools_anthropic_to_openai,
    extract_tool_results,
)
from .logger import _d, _dr
from .redact import redact, redact_headers
from .session_log import write_debug_json, write_error_file, write_warn_file
from .streaming import (
    _sse_write,
    _write_sse_error_native,
    relay_sse,
    stream_openai_to_anthropic,
)
from .tracer import _lookup_tool_use_name, _lookup_tool_use_producer, _trace


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
            _d(
                f"[CLIENT_GONE] {client_address}: {exc_type.__name__ if exc_type else '?'}: {exc_value}"
            )
            return
        # Любая другая ошибка — стандартное поведение (полный traceback в лог)
        super().handle_error(request, client_address)


_req_ctx = threading.local()


class Adapter(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        rid = getattr(_req_ctx, "req_id", "-")
        _d(f"[{rid}] [HTTP] {fmt % args}")

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
            _d(
                f"[CLIENT_GONE] {type(e).__name__} during sending status={status}: client disconnected before response could be sent"
            )
        except Exception as e:
            _d(f"[WARN] Error sending response: {type(e).__name__}: {e}")

    def _send_raw(self, status, content_type, body):
        """Отправляет клиенту ответ бэкенда ДОСЛОВНО: статус, Content-Type
        и тело — как пришли (passthrough E→E, non-stream). Никакой
        пере-сериализации: байты body пишутся как есть."""
        try:
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            _d(
                f"[CLIENT_GONE] {type(e).__name__} during sending status={status}: client disconnected before response could be sent"
            )
        except Exception as e:
            _d(f"[WARN] Error sending response: {type(e).__name__}: {e}")

    def _start_sse(self, status=200):
        """Отправляет заголовки SSE-ответа. После вызова этой функции
        заголовки уже ушли клиенту — откатиться на обычный JSON-ответ
        (например, чтобы вернуть 502 после неудачи) больше нельзя, поэтому
        вызывается только один раз, непосредственно перед первым событием
        стрима (см. ветку stream_requested в do_POST).

        ВАЖНО про Connection: этот адаптер нигде не переопределяет
        BaseHTTPRequestHandler.protocol_version, он остаётся дефолтным
        "HTTP/1.0". При таком protocol_version http.server ВСЕГДА
        закрывает TCP-соединение сразу после ответа (self.close_connection
        остаётся True) — независимо от того, что написано в заголовке
        Connection (см. BaseHTTPRequestHandler.parse_request: keep-alive
        честно применяется только если ОДНОВРЕМЕННО версия запроса клиента
        >= HTTP/1.1 И self.protocol_version >= "HTTP/1.1"). Раньше здесь
        стояло "Connection: keep-alive" — адаптер лгал клиенту, что
        соединение можно переиспользовать, а сам тут же его рвал. Node/
        Stainless-клиент Claude Code этому заголовку верил, клал сокет в
        пул на повторное использование, а при следующей попытке отправить
        по нему запрос получал ECONNRESET на уже закрытый сокет — именно
        это и порождало "API Error: The operation timed out" и
        "will retry in Xm Ys" в терминале уже ПОСЛЕ того, как адаптер
        успешно отдал предыдущий ответ (см. changelog/разбор лога).
        Отправляем "Connection: close", что соответствует тому, что
        сервер реально делает, и явно фиксируем self.close_connection —
        без опоры на дефолт, чтобы не сломать это неявно при будущих
        правках."""
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_HEAD(self):
        # Кто-то (health-check / сетевой пробник) стучится HEAD-запросами на
        # произвольные пути вроде /api/hello. Базовый BaseHTTPRequestHandler
        # не умеет HEAD и отвечает 501 через send_error(), что и породило
        # второй BrokenPipeError в логе. Отвечаем простым 200 без тела —
        # этого достаточно для любого health-check'а.
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/v1/models":
            # Возвращаем список моделей, полученный от бэкенда.
            # Формат — OpenAI-совместимый: {object: "list", data: [...]}.
            if not _AVAILABLE_MODELS:
                self._send_json(
                    501,
                    {
                        "error": "Model list not yet available. "
                        "Backend models have not been probed successfully yet."
                    },
                )
                return
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": list(_AVAILABLE_MODELS.values()),
                },
            )
        else:
            self._send_json(404, {"error": f"Unknown GET path: {self.path}"})

    def do_POST(self):
        req_t0 = time.time()
        session_id = self.headers.get("X-Claude-Code-Session-Id", "unknown")
        req_id = uuid.uuid4().hex[:12]

        # Учёт usage (таблица WEBUI «Использованные модели», колонки
        # Input/Output): локальные аккумуляторы токенов из usage-блоков
        # ответов бэкенда на время запроса, фиксация — в finally (см. внизу
        # do_POST). Активен только для запросов, прошедших strict-проверку
        # (usage_active ставится после record_model_usage) — 400-пути в
        # счётчики не попадают.
        usage_tokens = {"input": 0, "output": 0}
        usage_active = False

        _req_ctx.req_id = req_id
        _req_ctx.session_id = session_id
        try:
            # Обновляем глобальный fallback для _d(), чтобы человекочитаемый
            # лог тоже писался в правильный сессионный файл.
            session_log._last_log_session_id = session_id

            _d(f"\n{'=' * 70}")
            _dr(req_id, f"[REQ] {self.command} {self.path} session={session_id}")
            if config.ADAPTER_SENSITIVE_LOGGING_ENABLE:
                for k, v in self.headers.items():
                    _dr(req_id, f"  {k}: {v}")
            else:
                for k, v in redact_headers(self.headers).items():
                    _dr(req_id, f"  {k}: {v}")

            # === Диспетчер входных эндпоинтов (v0.9.0) ===
            # Три входных POST-пути — /v1/messages, /v1/chat/completions,
            # /v1/responses — по таблице routing.INPUT_PATHS (зеркало
            # ENDPOINT_PROBES). Любой другой путь — не вход адаптера: 404
            # с прежним текстом контракта (тест 404-контракта не меняется).
            inp_fmt = routing.input_path_to_format(self.path)
            if inp_fmt is None:
                self._send_json(404, {"error": "Expected /v1/messages"})
                return

            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            _body = body.decode()
            # Полная строка: консоль обрежет её до ADAPTER_DEBUG_TRIM в
            # logger._write, файл при ADAPTER_DEBUG_ENABLE=1 получит полную.
            _dr(req_id, f"[BODY] {_body}")
            if config.ADAPTER_DEBUG_PARTS:
                write_debug_json(session_id, "BODY", _body)

            try:
                anthropic_req = json.loads(body)
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"Invalid JSON: {e}"})
                return

            model = anthropic_req.get("model")
            if not model:
                self._send_json(400, {"error": "Missing required field: model"})
                return

            # Валидация имени модели (до маппинга и разрешения бэкенда):
            # имя из запроса клиента — именно оно указано в _AVAILABLE_MODELS
            # (с возможными префиксами бэкендов для коллизирующих моделей).
            client_model = model
            if (
                config.ADAPTER_STRICT_MODELS
                and _AVAILABLE_MODELS
                and client_model not in _AVAILABLE_MODELS
            ):
                available = list(_AVAILABLE_MODELS.keys())
                msg = (
                    f"Model '{client_model}' is not available. "
                    f"Allowed models: {', '.join(available)}"
                )
                _dr(req_id, f"[ERROR] {msg}")
                self._send_json(400, {"error": msg})
                return

            # === Модельный маппинг (agent-facing name -> backend name) ===
            original_model = client_model
            if _MAP and client_model in _MAP:
                model = _MAP[client_model]
                _dr(req_id, f"[MODEL_MAP] {original_model} -> {model}")
                _trace(
                    session_id, req_id, "model_map", agent_model=original_model, backend_model=model
                )
            else:
                model = client_model

            # Resolve backend для (возможно маппированного) имени.
            backend_cfg, resolved_model = _resolve_backend(model)
            backend_name = backend_cfg["name"]
            _dr(
                req_id,
                f"[BACKEND_RESOLVE] model={client_model} -> backend={backend_name}/{resolved_model}",
            )

            # === Решение о маршруте (routing.decide) ===
            # action:
            #   disabled — вход выключен (TARGET=none) → 404 (ДО учёта usage,
            #              как 400-пути: запрос до бэкенда не дошёл);
            #   reject   — маршрута нет (нереализованная пара / auto без
            #              пути / бэкенд подтверждённо не поддерживает
            #              целевой формат) → 400|502, тоже до учёта;
            #   passthrough — тело запроса уходит бэкенду КАК ЕСТЬ (E→E);
            #   convert  — реализованная конверсия (messages→completions).
            route = routing.decide(inp_fmt, backend_name)
            route_action, out_fmt, route_msg, route_status = route
            _dr(
                req_id,
                f"[ROUTE] input={inp_fmt} backend={backend_name} "
                f"target={routing.target_for_input(inp_fmt)} -> "
                f"{route_action}"
                + (f" (out={out_fmt})" if out_fmt else "")
                + (f": {route_msg}" if route_msg else ""),
            )
            _trace(
                session_id,
                req_id,
                "route_decide",
                input=inp_fmt,
                backend=backend_name,
                target=routing.target_for_input(inp_fmt),
                action=route_action,
                output=out_fmt,
                error=route_msg or None,
                http_status=route_status,
            )
            if route_action in ("disabled", "reject"):
                _dr(req_id, f"[ROUTE_REJECT] {route_msg}")
                self._send_json(route_status, {"error": route_msg})
                return

            # Маршрут определён: format'ы исходящего запроса (convert → out,
            # passthrough E→E → сам вход) и признак passthrough (тело не
            # пересобирается — см. ветку ниже).
            passthrough = route_action == "passthrough"
            if route_action == "convert":
                assert out_fmt is not None
                out_fmt_val = out_fmt
            else:
                out_fmt_val = inp_fmt

            # Учёт использованной модели (таблица WEBUI «Использованные
            # модели»): клиентское имя ДО маппинга; при первом обращении —
            # синхронная проба эндпоинтов этой моделью (резолв внутри
            # функции). Ставится ПОСЛЕ решения о маршруте — disabled/reject
            # (запрос до бэкенда не дошёл) в счётчики не попадают, как
            # 400-пути. Учёт не влияет на запрос: провал пробы не роняет его.
            model_usage.record_model_usage(client_model)
            usage_active = True

            # resolved_model — имя модели, которое пойдёт на бэкенд (без префикса)
            model = resolved_model

            # === Passthrough E→E (v0.9.0) ===
            # Тело запроса уходит бэкенду КАК ПРИШЛО — единственные мутации:
            # ``model`` заменяется на resolved_model (маппинг/префикс бэкенда
            # применяются и к passthrough) и, при аварийном рубильнике
            # ADAPTER_STREAMING_ENABLE=0, ``stream`` принудительно гасится.
            # Конвертация, strict-поля messages (max_tokens/system/tools/…) и
            # антропик-трассировка сюда не входят: контракт формата E→E
            # определяет бэкенд (он сам ответит 400 на невалидное тело), а
            # не адаптер. Ответ бэкенда отдаётся клиенту ДОСЛОВНО (см.
            # passthrough-хвост ниже) — usage читается по формату ``out_fmt``.
            if passthrough:
                _dr(
                    req_id,
                    f"[PASSTHROUGH] input={inp_fmt} target={routing.target_for_input(inp_fmt)} "
                    f"-> backend={backend_name}/{model} (out={out_fmt_val})",
                )
                stream_requested = bool(anthropic_req.get("stream", False))
                if stream_requested and not config.ADAPTER_STREAMING_ENABLE:
                    # Тот же рубильник, что у convert-ветки ниже: клиент просил
                    # stream=true, но стриминг выключен — форсим stream=false.
                    anthropic_req["stream"] = False
                    stream_requested = False
                    _dr(req_id, "[STREAM_DISABLED] (passthrough) forcing stream=false")
                anthropic_req["model"] = model
                _trace(
                    session_id,
                    req_id,
                    "request_start",
                    path=self.path,
                    model=model,
                    input_format=inp_fmt,
                    passthrough=True,
                    stream_requested=stream_requested,
                )
                _dr(
                    req_id,
                    f"[OPENAI_BODY] passthrough body={json.dumps(anthropic_req, ensure_ascii=False)}",
                )
                if config.ADAPTER_DEBUG_PARTS:
                    write_debug_json(session_id, "OPENAI_BODY", anthropic_req)
                backend_url = backend_cfg["base"].rstrip("/") + routing.INPUT_PATHS[out_fmt_val]
                backend_key_val = backend_cfg["key"]
                out_body = json.dumps(anthropic_req, ensure_ascii=False).encode()
                _dr(req_id, f"[BACKEND_URL] {backend_url}")
                req = urllib.request.Request(
                    backend_url,
                    data=out_body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {backend_key_val}",
                        "Connection": "keep-alive",
                    },
                    method="POST",
                )

                if stream_requested:
                    # === Passthrough-стрим (SSE-релей E→E) ===
                    # Поток бэкенда передаётся клиенту ДОСЛОВНО, построчно
                    # (relay_sse), без конвертации: контракт формата входа
                    # определяет бэкенд, адаптер лишь ретранслирует байты
                    # как есть (никакого пере-фрейминга/пере-сериализации).
                    # Retry возможен только ПОКА ни один байт не ушёл клиенту
                    # — после self._start_sse() откатиться нельзя (клиент уже
                    # получил заголовки и, возможно, часть событий), поэтому
                    # retry действует только на этапе urlopen(), а сбой уже во
                    # время релея обрабатывается SSE-событием "error" в родном
                    # формате входа (_write_sse_error_native), без retry.
                    # Маркер последней ошибки — int (HTTP-код) ИЛИ str
                    # ("timeout"/"error"); явная аннотация нужна mypy, чтобы
                    # не сузить переменную по первому присваиванию (e.code,
                    # int) и не ругаться на последующие строковые маркеры.
                    pt_stream_error: tuple[int | str, str] | None = None
                    pt_started = False
                    for attempt in range(1, ADAPTER_RETRY + 1):
                        try:
                            _dr(
                                req_id,
                                f"[FETCH] (passthrough stream) Attempt {attempt}/{ADAPTER_RETRY}, "
                                f"timeout={ADAPTER_TIMEOUT}s",
                            )
                            _trace(
                                session_id,
                                req_id,
                                "backend_attempt",
                                attempt=attempt,
                                timeout=ADAPTER_TIMEOUT,
                                streaming=True,
                                passthrough=True,
                            )
                            t0 = time.time()
                            resp = urllib.request.urlopen(
                                req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT
                            )
                            _dr(
                                req_id,
                                f"[FETCH] (passthrough stream) Заголовки получены за "
                                f"{time.time() - t0:.1f}s, status={resp.status}",
                            )
                            _trace(
                                session_id,
                                req_id,
                                "backend_result",
                                attempt=attempt,
                                ok=True,
                                status=resp.status,
                                elapsed_ms=int((time.time() - t0) * 1000),
                                passthrough=True,
                            )

                            self._start_sse(200)
                            pt_started = True
                            # relay_sse сам пишет байты в wfile и сканирует
                            # проходящие строки на usage-блоки (ответ в родном
                            # формате входа: completions — usage-поле финального
                            # чанка, responses/messages — событие response.completed
                            # / usage-событие). Возврат — нормализованные
                            # input_tokens/output_tokens (или {}).
                            pt_usage = relay_sse(resp, self.wfile, req_id, inp_fmt)
                            if pt_usage.get("input_tokens") or pt_usage.get("output_tokens"):
                                usage_tokens["input"] += int(pt_usage.get("input_tokens") or 0)
                                usage_tokens["output"] += int(pt_usage.get("output_tokens") or 0)
                            _dr(req_id, f"[OK] Passthrough stream done (out={out_fmt_val})")
                            _trace(
                                session_id,
                                req_id,
                                "request_end",
                                http_status=200,
                                retries_used=attempt - 1,
                                total_elapsed_ms=int((time.time() - req_t0) * 1000),
                                streamed=True,
                                passthrough=True,
                            )
                            return  # Успех -- выходим

                        except urllib.error.HTTPError as e:
                            err_raw = e.read()
                            err = err_raw.decode()
                            _dr(
                                req_id,
                                f"[BACKEND_ERR] HTTP {e.code} on attempt {attempt}: {err[:1500]}",
                            )
                            error_value = (
                                err[:500]
                                if config.ADAPTER_SENSITIVE_LOGGING_ENABLE
                                else redact(err[:500])
                            )
                            _trace(
                                session_id,
                                req_id,
                                "backend_result",
                                attempt=attempt,
                                ok=False,
                                status=e.code,
                                error=error_value,
                            )
                            pt_stream_error = (e.code, err)
                            # HTTP-ошибки (4xx) retry не делаем, кроме 429/503/504
                            if e.code not in (429, 502, 503, 504):
                                break
                            if attempt < ADAPTER_RETRY:
                                delay = 2**attempt
                                _dr(req_id, f"[RETRY] Waiting {delay}s...")
                                time.sleep(delay)

                        except TimeoutError as e:
                            _dr(
                                req_id,
                                f"[TIMEOUT] Attempt {attempt}/{ADAPTER_RETRY} timed out after "
                                f"{ADAPTER_TIMEOUT}s",
                            )
                            _trace(
                                session_id,
                                req_id,
                                "backend_result",
                                attempt=attempt,
                                ok=False,
                                status="timeout",
                                error=str(e),
                            )
                            pt_stream_error = ("timeout", str(e))
                            if attempt < ADAPTER_RETRY:
                                delay = 2**attempt
                                _dr(req_id, f"[RETRY] Waiting {delay}s before next attempt...")
                                time.sleep(delay)

                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
                            # Клиент отвалился сам (по своим причинам, не по
                            # нашей вине) прямо во время релея -- как и в
                            # convert-стрим-ветке, это не ошибка адаптера и
                            # логировать полный traceback не нужно.
                            _dr(
                                req_id,
                                f"[CLIENT_GONE] {type(e).__name__} while streaming: "
                                "client disconnected",
                            )
                            _trace(
                                session_id,
                                req_id,
                                "request_end",
                                http_status=None,
                                retries_used=attempt - 1,
                                total_elapsed_ms=int((time.time() - req_t0) * 1000),
                                streamed=True,
                                client_gone=True,
                                passthrough=True,
                            )
                            return

                        except Exception as e:
                            _dr(
                                req_id,
                                f"[FETCH_ERR] Attempt {attempt}/{ADAPTER_RETRY}: "
                                f"{type(e).__name__}: {e}",
                            )
                            _trace(
                                session_id,
                                req_id,
                                "backend_result",
                                attempt=attempt,
                                ok=False,
                                status="error",
                                error=f"{type(e).__name__}: {e}",
                            )
                            pt_stream_error = ("error", str(e))
                            if pt_started:
                                # Заголовки и часть событий уже ушли клиенту --
                                # откат невозможен. SSE-событие "error" в родном
                                # формате входа (клиент умеет разбирать его без
                                # конвертации), повторной попытки не будет
                                # (она породила бы второй поток внутри уже
                                # начатого ответа).
                                _write_sse_error_native(
                                    self.wfile, inp_fmt, f"{type(e).__name__}: {e}"
                                )
                                _trace(
                                    session_id,
                                    req_id,
                                    "request_end",
                                    http_status=200,
                                    retries_used=attempt - 1,
                                    total_elapsed_ms=int((time.time() - req_t0) * 1000),
                                    streamed=True,
                                    failed_mid_stream=True,
                                    passthrough=True,
                                )
                                return
                            if attempt < ADAPTER_RETRY:
                                delay = 2**attempt
                                _dr(req_id, f"[RETRY] Waiting {delay}s...")
                                time.sleep(delay)

                    # Все попытки исчерпаны, поток так и не начался
                    # (pt_started=False) -- заголовки ещё не отправлены, можно
                    # вернуть обычный JSON-error статус, как в non-stream
                    # ветке passthrough ниже.
                    if pt_stream_error and not pt_started:
                        code, msg = pt_stream_error
                        if code == "timeout":
                            _dr(
                                req_id,
                                f"[FAIL] All {ADAPTER_RETRY} attempts timed out. Returning 504.",
                            )
                            self._send_json(
                                504,
                                {"error": f"Gateway timeout after {ADAPTER_RETRY} attempts: {msg}"},
                            )
                            final_status = 504
                        elif isinstance(code, int):
                            _dr(req_id, f"[FAIL] Backend returned HTTP {code}. Returning {code}.")
                            self._send_json(code, {"error": f"Backend error: {msg}"})
                            final_status = code
                        else:
                            _dr(
                                req_id,
                                f"[FAIL] Returning 502 after {ADAPTER_RETRY} attempts.",
                            )
                            self._send_json(
                                502,
                                {
                                    "error": f"Backend unavailable after {ADAPTER_RETRY} attempts: {msg}"
                                },
                            )
                            final_status = 502
                        _trace(
                            session_id,
                            req_id,
                            "request_end",
                            http_status=final_status,
                            retries_used=ADAPTER_RETRY,
                            total_elapsed_ms=int((time.time() - req_t0) * 1000),
                            failed=True,
                            streamed=True,
                            passthrough=True,
                        )
                        # Протокол .err-инцидентов: финальный ответ — ошибка
                        # 4xx/5xx, запрос дошёл до бэкенда (out_body
                        # сформирован). Файл пишется БЕЗУСЛОВНО.
                        write_error_file(
                            session_id,
                            req_id,
                            final_status=final_status,
                            backend_url=backend_url,
                            model=model,
                            out_body=out_body,
                            err_body=msg,
                        )
                    return

                # === Passthrough non-stream ===
                # Retry loop (копия общей схемы convert-ветки): ждём ответ
                # бэкенда целиком и отдаём клиенту ДОСЛОВНО (статус и
                # Content-Type как у бэкенда, тело без конвертации).
                # Маркер последней ошибки — int (HTTP-код) ИЛИ str
                # ("timeout"/"error"); явная аннотация нужна, чтобы mypy не
                # сузил переменную по первому присваиванию (паттерн
                # convert-ветки ниже).
                pt_last_error: tuple[int | str, str] | None = None
                for attempt in range(1, ADAPTER_RETRY + 1):
                    try:
                        _dr(
                            req_id,
                            f"[FETCH] (passthrough) Attempt {attempt}/{ADAPTER_RETRY}, "
                            f"timeout={ADAPTER_TIMEOUT}s",
                        )
                        _trace(
                            session_id,
                            req_id,
                            "backend_attempt",
                            attempt=attempt,
                            timeout=ADAPTER_TIMEOUT,
                            passthrough=True,
                        )
                        t0 = time.time()
                        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT)
                        raw = resp.read()
                        elapsed = time.time() - t0
                        _dr(
                            req_id,
                            f"[FETCH] (passthrough) Success in {elapsed:.1f}s, {resp.status}, "
                            f"{len(raw)} bytes",
                        )
                        _dr(req_id, f"[FETCH_RAW] {raw.decode()}")
                        if config.ADAPTER_DEBUG_PARTS:
                            write_debug_json(session_id, "FETCH_RAW", raw.decode())
                        _trace(
                            session_id,
                            req_id,
                            "backend_result",
                            attempt=attempt,
                            ok=True,
                            status=resp.status,
                            elapsed_ms=int(elapsed * 1000),
                            passthrough=True,
                        )

                        # Usage по формату исходящего ответа (out_fmt_val):
                        # completions — usage.prompt_tokens/completion_tokens,
                        # responses — usage.input_tokens/output_tokens,
                        # messages — usage.input_tokens/output_tokens.
                        try:
                            o = json.loads(raw)
                        except json.JSONDecodeError:
                            o = None
                        if isinstance(o, dict):
                            u = o.get("usage") or {}
                            if out_fmt_val == "completions":
                                u_in, u_out = u.get("prompt_tokens"), u.get("completion_tokens")
                            else:
                                u_in, u_out = u.get("input_tokens"), u.get("output_tokens")
                            if u_in or u_out:
                                usage_tokens["input"] += int(u_in or 0)
                                usage_tokens["output"] += int(u_out or 0)

                        resp_body = raw.decode()
                        if config.ADAPTER_DEBUG_PARTS:
                            write_debug_json(session_id, "RESPONSE", resp_body)
                        # Тело дословно (raw), статус и Content-Type бэкенда.
                        self._send_raw(resp.status, resp.headers.get("Content-Type"), raw)
                        _dr(
                            req_id,
                            f"[OK] Passthrough done (out={out_fmt_val}), status={resp.status}",
                        )
                        _trace(
                            session_id,
                            req_id,
                            "request_end",
                            http_status=resp.status,
                            retries_used=attempt - 1,
                            total_elapsed_ms=int((time.time() - req_t0) * 1000),
                            passthrough=True,
                        )
                        return  # Успех -- выходим

                    except urllib.error.HTTPError as e:
                        err_raw = e.read()
                        err = err_raw.decode()
                        _dr(
                            req_id,
                            f"[BACKEND_ERR] HTTP {e.code} on attempt {attempt}: {err[:1500]}",
                        )
                        error_value = (
                            err[:500]
                            if config.ADAPTER_SENSITIVE_LOGGING_ENABLE
                            else redact(err[:500])
                        )
                        _trace(
                            session_id,
                            req_id,
                            "backend_result",
                            attempt=attempt,
                            ok=False,
                            status=e.code,
                            error=error_value,
                        )
                        pt_last_error = (e.code, err)
                        # HTTP-ошибки (4xx) retry не делаем, кроме 429/503/504
                        if e.code not in (429, 502, 503, 504):
                            break
                        if attempt < ADAPTER_RETRY:
                            delay = 2**attempt
                            _dr(req_id, f"[RETRY] Waiting {delay}s...")
                            time.sleep(delay)

                    except TimeoutError as e:
                        _dr(
                            req_id,
                            f"[TIMEOUT] Attempt {attempt}/{ADAPTER_RETRY} timed out after {ADAPTER_TIMEOUT}s",
                        )
                        _trace(
                            session_id,
                            req_id,
                            "backend_result",
                            attempt=attempt,
                            ok=False,
                            status="timeout",
                            error=str(e),
                        )
                        pt_last_error = ("timeout", str(e))
                        if attempt < ADAPTER_RETRY:
                            delay = 2**attempt
                            _dr(req_id, f"[RETRY] Waiting {delay}s before next attempt...")
                            time.sleep(delay)

                    except Exception as e:
                        _dr(
                            req_id,
                            f"[FETCH_ERR] Attempt {attempt}/{ADAPTER_RETRY}: {type(e).__name__}: {e}",
                        )
                        _trace(
                            session_id,
                            req_id,
                            "backend_result",
                            attempt=attempt,
                            ok=False,
                            status="error",
                            error=f"{type(e).__name__}: {e}",
                        )
                        pt_last_error = ("error", str(e))
                        if attempt < ADAPTER_RETRY:
                            delay = 2**attempt
                            _dr(req_id, f"[RETRY] Waiting {delay}s...")
                            time.sleep(delay)

                # Все попытки исчерпаны — как convert-ветка: 504/код/502.
                if pt_last_error:
                    code, msg = pt_last_error
                    if code == "timeout":
                        _dr(
                            req_id, f"[FAIL] All {ADAPTER_RETRY} attempts timed out. Returning 504."
                        )
                        self._send_json(
                            504, {"error": f"Gateway timeout after {ADAPTER_RETRY} attempts: {msg}"}
                        )
                        final_status = 504
                    elif isinstance(code, int):
                        _dr(req_id, f"[FAIL] Backend returned HTTP {code}. Returning {code}.")
                        self._send_json(code, {"error": f"Backend error: {msg}"})
                        final_status = code
                    else:
                        _dr(req_id, f"[FAIL] Returning 502 after {ADAPTER_RETRY} attempts.")
                        self._send_json(
                            502,
                            {"error": f"Backend unavailable after {ADAPTER_RETRY} attempts: {msg}"},
                        )
                        final_status = 502
                    _trace(
                        session_id,
                        req_id,
                        "request_end",
                        http_status=final_status,
                        retries_used=ADAPTER_RETRY,
                        total_elapsed_ms=int((time.time() - req_t0) * 1000),
                        failed=True,
                        passthrough=True,
                    )
                    write_error_file(
                        session_id,
                        req_id,
                        final_status=final_status,
                        backend_url=backend_url,
                        model=model,
                        out_body=out_body,
                        err_body=msg,
                    )
                return

            max_tokens = anthropic_req.get("max_tokens", 4096)
            in_tools = anthropic_req.get("tools", [])
            in_tool_names = [t.get("name", "?") for t in in_tools]
            in_messages = anthropic_req.get("messages", [])
            has_structured_output = bool((anthropic_req.get("output_config") or {}).get("format"))

            # request_kind — СТРУКТУРНЫЙ (не по содержимому промпта) признак
            # того, к какому потоку в рамках сессии относится запрос:
            #   agent_turn        -- обычный ход основного агентного цикла
            #                        (есть хотя бы один tool)
            #   structured_output -- сайдкар-вызов вроде генерации заголовка
            #                        сессии: 0 tools + output_config.format
            #                        задан. Идёт параллельно основному циклу,
            #                        НЕ является его веткой, хотя делит тот же
            #                        session_id.
            #   plain              -- ни тулов, ни structured output (редкий
            #                        случай, например самый первый system-title
            #                        запрос без tools).
            if in_tools:
                request_kind = "agent_turn"
            elif has_structured_output:
                request_kind = "structured_output"
            else:
                request_kind = "plain"

            # Требовал ли исходный запрос стриминг. РАНЬШЕ здесь стоял
            # комментарий "адаптер всегда шлёт backend'у stream=False независимо
            # от этого значения" -- именно это и было первопричиной
            # BrokenPipeError (см. stream_openai_to_anthropic и разбор ниже):
            # пока backend не отдаст ответ целиком, клиент не получает ни
            # байта и рвёт сокет по своему таймауту. Теперь адаптер честно
            # пробрасывает клиентский флаг stream дальше в backend_stream и
            # либо стримит SSE построчно (см. ветку stream_requested в конце
            # do_POST), либо, если клиент явно не просил стрим, работает как
            # раньше -- ждёт полный ответ.
            stream_requested = bool(anthropic_req.get("stream", False))
            if stream_requested and not config.ADAPTER_STREAMING_ENABLE:
                # Аварийный рубильник ADAPTER_STREAMING_ENABLE=0 -- принудительно
                # откатываемся к старому поведению, даже если клиент просил
                # stream=true. См. комментарий у переменной выше.
                _dr(
                    req_id,
                    "[STREAM_DISABLED] Клиент просил stream=true, но ADAPTER_STREAMING_ENABLE=0 -> forcing backend_stream=False",
                )
                stream_requested = False
            _dr(
                req_id,
                f"[STREAM_REQUESTED] anthropic_stream={anthropic_req.get('stream')} -> backend_stream={stream_requested} (streaming_mode={'on' if config.ADAPTER_STREAMING_ENABLE else 'off'})",
            )

            _trace(
                session_id,
                req_id,
                "request_start",
                path=self.path,
                model=model,
                max_tokens=max_tokens,
                msg_count=len(in_messages),
                tool_count=len(in_tools),
                tool_names=in_tool_names,
                tool_choice=anthropic_req.get("tool_choice"),
                request_kind=request_kind,
                stream_requested=stream_requested,
            )

            # === Трассировка исполнения инструмента (tool_result) ===
            # Делается ДО конвертации в OpenAI-формат и ДО отправки бэкенду --
            # это чисто наблюдение за тем, что харнесс уже прислал нам в этом
            # запросе. Каждый tool_result связывается с req_id запроса, который
            # породил соответствующий tool_use (см. _register_tool_use в
            # convert_openai_to_anthropic), через уникальный tool_use_id.
            for tr in extract_tool_results(in_messages):
                parent_req_id = _lookup_tool_use_producer(session_id, tr["tool_use_id"])
                traced_content = tr["content"]
                if config.ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS > 0:
                    traced_content = _cap(traced_content, config.ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS)
                _trace(
                    session_id,
                    req_id,
                    "tool_result",
                    tool_use_id=tr["tool_use_id"],
                    # parent_req_id=None означает, что производитель этого
                    # tool_use не найден в индексе данной сессии -- либо он
                    # был вытеснен по FIFO (см. _TOOL_USE_INDEX_MAX_PER_SESSION),
                    # либо tool_use случился до перезапуска адаптера/вне
                    # трассировки. Само по себе это не ошибка, но означает,
                    # что причинная связь для данного узла восстановить
                    # нельзя -- только его содержимое.
                    parent_req_id=parent_req_id,
                    is_error=tr["is_error"],
                    content=traced_content,
                )
                _tool_name = _lookup_tool_use_name(session_id, tr["tool_use_id"])
                _dr(
                    req_id,
                    f"[TOOL_RESULT] tool_name={_tool_name or '?'} tool_use_id={tr['tool_use_id']} parent_req_id={parent_req_id} is_error={tr['is_error']} len={len(tr['content'])}",
                )
                # Content-блок — БЕЗУСЛОВНО для каждого результата (ошибки НЕ
                # выделяются отдельным [TOOL_RESULT_ERROR]-блоком): единый блок для
                # всех, полный JSON; консоль обрежет TRIM, файл при
                # ADAPTER_DEBUG_ENABLE=1 получит полный. Отдельного дампа под
                # ADAPTER_DEBUG_PARTS нет — файловую часть несёт тот же
                # консольный блок (см. ниже дамп TOOL_RESULT для *.parts).
                _dr(
                    req_id,
                    f"[TOOL_RESULT] content={json.dumps(tr['content'], ensure_ascii=False, default=str)}",
                )
                if config.ADAPTER_DEBUG_PARTS:
                    # *.parts-дамп — ОДИН на результат, полный dict (включая
                    # content целиком): тег несёт часть полностью.
                    write_debug_json(
                        session_id,
                        "TOOL_RESULT",
                        {
                            "tool_use_id": tr["tool_use_id"],
                            "tool_name": _tool_name or "",
                            "parent_req_id": parent_req_id,
                            "is_error": tr["is_error"],
                            "content": tr["content"],
                        },
                    )

            openai_body = {
                "model": model,
                "messages": convert_messages_anthropic_to_openai(
                    anthropic_req.get("messages", []), anthropic_req.get("system")
                ),
                "max_tokens": max_tokens,
                # Пробрасываем клиентский флаг как есть (см. комментарий у
                # stream_requested выше) -- раньше тут было жёстко "stream": False.
                "stream": stream_requested,
            }

            if stream_requested and config.ADAPTER_STREAM_INCLUDE_USAGE:
                # Без этого поля OpenAI-совместимый стриминг НЕ присылает usage
                # ни в одном SSE-чанке -- см. комментарий у ADAPTER_STREAM_INCLUDE_USAGE.
                # Именно это было первопричиной input_tokens=0 в message_start/
                # message_delta и, как следствие, неверной оценки заполнения
                # контекстного окна агентом ТОЛЬКО в потоковом режиме.
                openai_body["stream_options"] = {"include_usage": True}

            if "tools" in anthropic_req:
                openai_body["tools"] = convert_tools_anthropic_to_openai(anthropic_req["tools"])
                _dr(req_id, f"[TOOLS] Passed {len(openai_body['tools'])} tools")

            if "tool_choice" in anthropic_req:
                openai_body["tool_choice"] = convert_tool_choice_anthropic_to_openai(
                    anthropic_req["tool_choice"]
                )
                _dr(req_id, f"[TOOL_CHOICE] {openai_body['tool_choice']}")

            # Проверяем, что system действительно в начале. Это диагностика
            # ИНВАРИАНТА КОНВЕРТАЦИИ самого адаптера (Anthropic->OpenAI), а не
            # решение модели или харнесса -- раньше это писалось под общим,
            # вводящим в заблуждение именем "harness_branch".
            msgs = openai_body["messages"]
            system_ok = bool(msgs) and msgs[0]["role"] == "system"
            _trace(
                session_id,
                req_id,
                "adapter_invariant_check",
                check="system_message_first",
                passed=system_ok,
                first_role=(msgs[0]["role"] if msgs else None),
            )
            if system_ok:
                _dr(req_id, "[CHECK] First message is system, OK")
            else:
                warn_role = msgs[0]["role"] if msgs else "empty"
                _dr(req_id, f"[WARN] First message is NOT system: {warn_role}")

            # Полная строка: консоль обрежет её до ADAPTER_DEBUG_TRIM в
            # logger._write, файл при ADAPTER_DEBUG_ENABLE=1 получит полную.
            _dr(
                req_id,
                f"[OPENAI_BODY] {json.dumps(openai_body, ensure_ascii=False)}",
            )

            if config.ADAPTER_DEBUG_PARTS:
                write_debug_json(session_id, "OPENAI_BODY", openai_body)

            # Построить URL и Authorization из resolved backend-конфига.
            # key -- уже раскрытый токен (раскрытие происходит в _parse_backend_yaml).
            # Тело сериализуется ОДИН раз в out_body (раньше — инлайн в
            # Request.data): те же байты переиспользуются всеми retry-
            # попытками.
            backend_url = backend_cfg["base"].rstrip("/") + "/v1/chat/completions"
            backend_key_val = backend_cfg["key"]
            out_body = json.dumps(openai_body, ensure_ascii=False).encode()
            if not system_ok:
                # WARN-событие инварианта конвертации — в .err-файл сессии
                # (v0.9.1): тот же безусловный канал, что у инцидентов 4xx/5xx,
                # но для диагностики на УСПЕШНОМ запросе. Пишется один раз на
                # запрос, здесь — после построения out_body/backend_url (нужны
                # для полного [REQUEST]-блока) и до urlopen (покрывает и
                # stream-, и non-stream-ветку). Полный запрос + репорт, без
                # TRIM, вне ADAPTER_DEBUG_ENABLE/PARTS.
                write_warn_file(
                    session_id,
                    req_id,
                    backend_url=backend_url,
                    model=model,
                    out_body=out_body,
                    warn_body=f"First message is NOT system: {warn_role}",
                )
            req = urllib.request.Request(
                backend_url,
                data=out_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {backend_key_val}",
                    "Connection": "keep-alive",
                },
                method="POST",
            )

            if stream_requested:
                # === Потоковая ветка ===
                # Retry возможен только ПОКА ни один байт не ушёл клиенту --
                # после self._start_sse()/первого content_block_start откатиться
                # на новую попытку уже нельзя (клиент получит два message_start
                # подряд), поэтому в этой ветке retry действует только на этапе
                # urlopen() (соединение/заголовки), а сбой уже во время самого
                # чтения SSE (stream_openai_to_anthropic бросит исключение)
                # обрабатывается отдельно -- событием SSE "error", без retry.
                # Тип маркера последней ошибки -- int (HTTP-код из e.code)
                # ИЛИ str ("timeout"/"error"); явная аннотация нужна, чтобы
                # mypy не сузил переменную по первому присваиванию (e.code,
                # int) и не ругался на последующие строковые маркеры.
                last_error: tuple[int | str, str] | None = None
                started = False
                for attempt in range(1, ADAPTER_RETRY + 1):
                    try:
                        _dr(
                            req_id,
                            f"[FETCH] (stream) Attempt {attempt}/{ADAPTER_RETRY}, timeout={ADAPTER_TIMEOUT}s",
                        )
                        _trace(
                            session_id,
                            req_id,
                            "backend_attempt",
                            attempt=attempt,
                            timeout=ADAPTER_TIMEOUT,
                            streaming=True,
                        )
                        t0 = time.time()
                        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT)
                        _dr(
                            req_id,
                            f"[FETCH] (stream) Заголовки получены за {time.time() - t0:.1f}s, status={resp.status}",
                        )
                        _trace(
                            session_id,
                            req_id,
                            "backend_result",
                            attempt=attempt,
                            ok=True,
                            status=resp.status,
                            elapsed_ms=int((time.time() - t0) * 1000),
                        )

                        self._start_sse(200)
                        started = True
                        approx_prompt_chars = len(
                            json.dumps(openai_body.get("messages", []), ensure_ascii=False)
                        )
                        stop_reason, usage = stream_openai_to_anthropic(
                            resp,
                            self.wfile,
                            model,
                            session_id,
                            req_id,
                            approx_prompt_chars=approx_prompt_chars,
                            # v0.9.1: для [USAGE_WARN] в .err — полный запрос
                            # и URL бэкенда (нужны, если usage не вернётся).
                            out_body=out_body,
                            backend_url=backend_url,
                        )
                        # usage — usage-блок последнего SSE-чанка (или {}):
                        # считаем токены только из реального usage, эвристика
                        # chars//4 (input_tokens_estimated) в учёт не попадает.
                        if usage.get("prompt_tokens") or usage.get("completion_tokens"):
                            usage_tokens["input"] += int(usage.get("prompt_tokens") or 0)
                            usage_tokens["output"] += int(usage.get("completion_tokens") or 0)
                        _dr(req_id, f"[OK] Stream done, stop_reason={stop_reason}")
                        _trace(
                            session_id,
                            req_id,
                            "request_end",
                            http_status=200,
                            retries_used=attempt - 1,
                            total_elapsed_ms=int((time.time() - req_t0) * 1000),
                            streamed=True,
                        )
                        return  # Успех -- выходим

                    except urllib.error.HTTPError as e:
                        err_raw = e.read()
                        err = err_raw.decode()
                        _dr(
                            req_id,
                            f"[BACKEND_ERR] HTTP {e.code} on attempt {attempt}: {err[:1500]}",
                        )
                        error_value = (
                            err[:500]
                            if config.ADAPTER_SENSITIVE_LOGGING_ENABLE
                            else redact(err[:500])
                        )
                        _trace(
                            session_id,
                            req_id,
                            "backend_result",
                            attempt=attempt,
                            ok=False,
                            status=e.code,
                            error=error_value,
                        )
                        last_error = (e.code, err)
                        if e.code not in (429, 502, 503, 504):
                            break
                        if attempt < ADAPTER_RETRY:
                            delay = 2**attempt
                            _dr(req_id, f"[RETRY] Waiting {delay}s...")
                            time.sleep(delay)

                    except TimeoutError as e:
                        _dr(
                            req_id,
                            f"[TIMEOUT] Attempt {attempt}/{ADAPTER_RETRY} timed out after {ADAPTER_TIMEOUT}s",
                        )
                        _trace(
                            session_id,
                            req_id,
                            "backend_result",
                            attempt=attempt,
                            ok=False,
                            status="timeout",
                            error=str(e),
                        )
                        last_error = ("timeout", str(e))
                        if attempt < ADAPTER_RETRY:
                            delay = 2**attempt
                            _dr(req_id, f"[RETRY] Waiting {delay}s before next attempt...")
                            time.sleep(delay)

                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
                        # Клиент отвалился сам (по своим причинам, не по нашей
                        # вине) прямо во время стрима -- как и в _send_json,
                        # это не ошибка адаптера и логировать полный traceback
                        # не нужно.
                        _dr(
                            req_id,
                            f"[CLIENT_GONE] {type(e).__name__} while streaming: client disconnected",
                        )
                        _trace(
                            session_id,
                            req_id,
                            "request_end",
                            http_status=None,
                            retries_used=attempt - 1,
                            total_elapsed_ms=int((time.time() - req_t0) * 1000),
                            streamed=True,
                            client_gone=True,
                        )
                        return

                    except Exception as e:
                        _dr(
                            req_id,
                            f"[FETCH_ERR] Attempt {attempt}/{ADAPTER_RETRY}: {type(e).__name__}: {e}",
                        )
                        _trace(
                            session_id,
                            req_id,
                            "backend_result",
                            attempt=attempt,
                            ok=False,
                            status="error",
                            error=f"{type(e).__name__}: {e}",
                        )
                        last_error = ("error", str(e))
                        if started:
                            # Заголовки и часть событий уже ушли клиенту --
                            # откатить нельзя. Сообщаем об обрыве SSE-событием
                            # "error" вместо повторной попытки (которая бы
                            # породила второй message_start внутри уже
                            # начатого ответа).
                            with contextlib.suppress(Exception):
                                _sse_write(
                                    self.wfile,
                                    "error",
                                    {
                                        "type": "error",
                                        "error": {
                                            "type": "api_error",
                                            "message": f"{type(e).__name__}: {e}",
                                        },
                                    },
                                )
                            _trace(
                                session_id,
                                req_id,
                                "request_end",
                                http_status=200,
                                retries_used=attempt - 1,
                                total_elapsed_ms=int((time.time() - req_t0) * 1000),
                                streamed=True,
                                failed_mid_stream=True,
                            )
                            return
                        if attempt < ADAPTER_RETRY:
                            delay = 2**attempt
                            _dr(req_id, f"[RETRY] Waiting {delay}s...")
                            time.sleep(delay)

                # Все попытки исчерпаны, поток так и не начался (started=False)
                # -- заголовки ещё не отправлены, можно вернуть обычный
                # JSON-error статус, как и в нестриминговой ветке ниже.
                if last_error and not started:
                    code, msg = last_error
                    if code == "timeout":
                        _dr(
                            req_id, f"[FAIL] All {ADAPTER_RETRY} attempts timed out. Returning 504."
                        )
                        self._send_json(
                            504, {"error": f"Gateway timeout after {ADAPTER_RETRY} attempts: {msg}"}
                        )
                        final_status = 504
                    elif isinstance(code, int):
                        _dr(req_id, f"[FAIL] Backend returned HTTP {code}. Returning {code}.")
                        self._send_json(code, {"error": f"Backend error: {msg}"})
                        final_status = code
                    else:
                        _dr(req_id, f"[FAIL] Returning 502 after {ADAPTER_RETRY} attempts.")
                        self._send_json(
                            502,
                            {"error": f"Backend unavailable after {ADAPTER_RETRY} attempts: {msg}"},
                        )
                        final_status = 502
                    _trace(
                        session_id,
                        req_id,
                        "request_end",
                        http_status=final_status,
                        retries_used=ADAPTER_RETRY,
                        total_elapsed_ms=int((time.time() - req_t0) * 1000),
                        failed=True,
                        streamed=True,
                    )
                    # Протокол .err-инцидентов (v0.9.0): финальный ответ —
                    # ошибка 4xx/5xx, запрос дошёл до бэкенда (out_body
                    # сформирован). Пишем файл инцидента БЕЗУСЛОВНО (не
                    # зависит от ADAPTER_DEBUG_ENABLE/PARTS/TRIM).
                    write_error_file(
                        session_id,
                        req_id,
                        final_status=final_status,
                        backend_url=backend_url,
                        model=model,
                        out_body=out_body,
                        err_body=msg,
                    )
                return

            # === Нестриминговая ветка (клиент явно не просил stream) ===
            # Retry loop -- оставлена как была: ждём ответ бэкенда целиком.
            # last_error уже ОБЪЯВЛЕН с типом в стрим-ветке выше (объявление
            # действует на всю функцию) -- здесь только присваивание, без
            # повторной аннотации: mypy счёл бы её переопределением (no-redef).
            last_error = None
            for attempt in range(1, ADAPTER_RETRY + 1):
                try:
                    _dr(
                        req_id,
                        f"[FETCH] Attempt {attempt}/{ADAPTER_RETRY}, timeout={ADAPTER_TIMEOUT}s",
                    )
                    _trace(
                        session_id,
                        req_id,
                        "backend_attempt",
                        attempt=attempt,
                        timeout=ADAPTER_TIMEOUT,
                    )
                    t0 = time.time()
                    resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT)
                    raw = resp.read()
                    elapsed = time.time() - t0
                    _dr(
                        req_id,
                        f"[FETCH] Success in {elapsed:.1f}s, {resp.status}, {len(raw)} bytes",
                    )
                    # Полная строка: консоль обрежет её до ADAPTER_DEBUG_TRIM
                    # в logger._write, файл при ENABLE=1 получит полную.
                    _dr(req_id, f"[FETCH_RAW] {raw.decode()}")
                    if config.ADAPTER_DEBUG_PARTS:
                        write_debug_json(session_id, "FETCH_RAW", raw.decode())
                    _trace(
                        session_id,
                        req_id,
                        "backend_result",
                        attempt=attempt,
                        ok=True,
                        status=resp.status,
                        elapsed_ms=int(elapsed * 1000),
                    )

                    o = json.loads(raw)
                    anthropic_resp = convert_openai_to_anthropic(o, model, session_id, req_id)

                    # Учёт usage: токены из usage-блока ответа бэкенда.
                    # Ответы без usage (ошибки, обрывы, бэкенд без usage) —
                    # 0/0, счётчики не трогаются.
                    u = o.get("usage") or {}
                    if u.get("prompt_tokens") or u.get("completion_tokens"):
                        usage_tokens["input"] += int(u.get("prompt_tokens") or 0)
                        usage_tokens["output"] += int(u.get("completion_tokens") or 0)

                    # Полная строка: консоль обрежет её до ADAPTER_DEBUG_TRIM
                    # в logger._write, файл при ENABLE=1 получит полную.
                    _dr(
                        req_id,
                        f"[RESPONSE] {json.dumps(anthropic_resp, ensure_ascii=False)}",
                    )
                    if config.ADAPTER_DEBUG_PARTS:
                        write_debug_json(session_id, "RESPONSE", anthropic_resp)
                    self._send_json(200, anthropic_resp)
                    _dr(req_id, "[OK] Done")
                    _trace(
                        session_id,
                        req_id,
                        "request_end",
                        http_status=200,
                        retries_used=attempt - 1,
                        total_elapsed_ms=int((time.time() - req_t0) * 1000),
                    )
                    return  # Успех -- выходим

                except urllib.error.HTTPError as e:
                    err_raw = e.read()
                    err = err_raw.decode()
                    _dr(req_id, f"[BACKEND_ERR] HTTP {e.code} on attempt {attempt}: {err[:1500]}")
                    error_value = (
                        err[:500] if config.ADAPTER_SENSITIVE_LOGGING_ENABLE else redact(err[:500])
                    )
                    _trace(
                        session_id,
                        req_id,
                        "backend_result",
                        attempt=attempt,
                        ok=False,
                        status=e.code,
                        error=error_value,
                    )
                    last_error = (e.code, err)
                    # HTTP-ошибки (4xx) retry не делаем, кроме 429/503/504
                    if e.code not in (429, 502, 503, 504):
                        break
                    if attempt < ADAPTER_RETRY:
                        delay = 2**attempt
                        _dr(req_id, f"[RETRY] Waiting {delay}s...")
                        time.sleep(delay)

                except TimeoutError as e:
                    _dr(
                        req_id,
                        f"[TIMEOUT] Attempt {attempt}/{ADAPTER_RETRY} timed out after {ADAPTER_TIMEOUT}s",
                    )
                    _trace(
                        session_id,
                        req_id,
                        "backend_result",
                        attempt=attempt,
                        ok=False,
                        status="timeout",
                        error=str(e),
                    )
                    last_error = ("timeout", str(e))
                    if attempt < ADAPTER_RETRY:
                        delay = 2**attempt
                        _dr(req_id, f"[RETRY] Waiting {delay}s before next attempt...")
                        time.sleep(delay)

                except Exception as e:
                    _dr(
                        req_id,
                        f"[FETCH_ERR] Attempt {attempt}/{ADAPTER_RETRY}: {type(e).__name__}: {e}",
                    )
                    _trace(
                        session_id,
                        req_id,
                        "backend_result",
                        attempt=attempt,
                        ok=False,
                        status="error",
                        error=f"{type(e).__name__}: {e}",
                    )
                    last_error = ("error", str(e))
                    if attempt < ADAPTER_RETRY:
                        delay = 2**attempt
                        _dr(req_id, f"[RETRY] Waiting {delay}s...")
                        time.sleep(delay)

            # Все попытки исчерпаны.
            # Тип final_status (int: HTTP-код для _trace) уже зафиксирован
            # присваиваниями в стрим-ветке выше; здесь все ветки присваивают
            # только int (504 / HTTP-код / 502), поэтому аннотация не нужна.
            if last_error:
                code, msg = last_error
                if code == "timeout":
                    _dr(req_id, f"[FAIL] All {ADAPTER_RETRY} attempts timed out. Returning 504.")
                    self._send_json(
                        504, {"error": f"Gateway timeout after {ADAPTER_RETRY} attempts: {msg}"}
                    )
                    final_status = 504
                elif isinstance(code, int):
                    _dr(req_id, f"[FAIL] Backend returned HTTP {code}. Returning {code}.")
                    self._send_json(code, {"error": f"Backend error: {msg}"})
                    final_status = code
                else:
                    # Маркер "error": timeout не был (иначе обработали выше),
                    # HTTP-кода нет (не был HTTPError) -- дошли сюда после
                    # исчерпания ретраев на общем исключении. Раньше этот
                    # случай НЕ отправлял клиенту ответ вовсе (мертвая строка
                    # final_status = code после elif), а в trace уходил
                    # строковый маркер вместо кода -- теперь честно отдаём
                    # 502, как в соседней ветке else.
                    _dr(req_id, f"[FAIL] Returning 502 after {ADAPTER_RETRY} attempts.")
                    self._send_json(
                        502,
                        {"error": f"Backend unavailable after {ADAPTER_RETRY} attempts: {msg}"},
                    )
                    final_status = 502
            else:
                # last_error пуст (недостижимый fallback после исчерпания
                # цикла без исключения) — msg для .err-файла задаём тем же
                # текстом, что уходит клиенту.
                msg = f"Backend unavailable after {ADAPTER_RETRY} attempts"
                _dr(req_id, f"[FAIL] Returning 502 after {ADAPTER_RETRY} attempts.")
                self._send_json(
                    502, {"error": f"Backend unavailable after {ADAPTER_RETRY} attempts: {msg}"}
                )
                final_status = 502
            _trace(
                session_id,
                req_id,
                "request_end",
                http_status=final_status,
                retries_used=ADAPTER_RETRY,
                total_elapsed_ms=int((time.time() - req_t0) * 1000),
                failed=True,
            )
            # Протокол .err-инцидентов (v0.9.0): финальный ответ — ошибка
            # 4xx/5xx, запрос дошёл до бэкенда. Файл инцидента пишется
            # БЕЗУСЛОВНО (вне ADAPTER_DEBUG_ENABLE/PARTS/TRIM). msg — полное
            # тело последней попытки (HTTPError) или текст исключения.
            write_error_file(
                session_id,
                req_id,
                final_status=final_status,
                backend_url=backend_url,
                model=model,
                out_body=out_body,
                err_body=msg,
            )
        finally:
            # Фиксация учёта usage (таблица WEBUI «Использованные модели»):
            # единственная точка записи на любой исход запроса (успех, ошибка,
            # исчерпание ретраев, исключение). Активно только для запросов,
            # прошедших strict-проверку; add_usage_tokens не бросает исключений.
            if usage_active:
                model_usage.add_usage_tokens(
                    client_model, usage_tokens["input"], usage_tokens["output"]
                )
            delattr(_req_ctx, "req_id")
            delattr(_req_ctx, "session_id")
