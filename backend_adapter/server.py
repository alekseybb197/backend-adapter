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

from . import config, model_usage, routing, session_log, session_registry, session_settings
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
    convert_openai_completions_to_responses,
    convert_openai_to_anthropic,
    convert_responses_input_to_openai_messages,
    convert_responses_tool_choice_to_openai,
    convert_responses_tools_to_openai,
    convert_tool_choice_anthropic_to_openai,
    convert_tools_anthropic_to_openai,
    detect_model_switch_command,
    extract_responses_tool_results,
    extract_tool_results,
    force_store_false,
    normalize_messages_system_first,
)
from .logger import _d, _dr
from .redact import redact, redact_headers
from .session_log import (
    UNKNOWN_SESSION_ID,
    write_debug_json,
    write_error_file,
    write_session_error,
    write_warn_file,
)
from .streaming import (
    _sse_write,
    _write_sse_error_native,
    build_responses_control_response,
    emit_responses_control_message,
    relay_sse,
    stream_openai_completions_to_responses,
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


def _parse_session_header_specs(raw: str) -> list[tuple[str, str | None]]:
    """Разбирает строку ``ADAPTER_SESSION_HEADER`` в пары (имя, JSON-ключ|None).

    Элемент списка — либо «Имя-заголовка» (плоский заголовок, ключ ``None``),
    либо «Имя-заголовка:ключ» (значение — JSON-объект, берётся строковое поле
    ``ключ``). Пустые элементы отброшены."""
    specs: list[tuple[str, str | None]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, key = item.partition(":")
        name = name.strip()
        if name:
            specs.append((name, key.strip() or None))
    return specs


def _session_header_names() -> list[tuple[str, str | None]]:
    """Кандидаты session id: пары (имя заголовка, JSON-ключ|None).

    Источник — ``ADAPTER_SESSION_HEADER``; полностью пустой список → дефолт."""
    specs = _parse_session_header_specs(config.ADAPTER_SESSION_HEADER)
    return specs or _parse_session_header_specs(config._DEFAULT_SESSION_HEADERS)


def _header_session_value(headers, name: str, key: str | None) -> str:
    """Значение одного кандидата: плоский заголовок либо поле JSON-заголовка.

    ``key is None`` — обычный заголовок. Иначе значение разбирается как
    JSON-объект и возвращается его строковое поле ``key``; битый JSON, не-объект,
    отсутствие ключа или нестроковое значение → ``""`` (кандидат пропускается)."""
    value = headers.get(name)
    if not value:
        return ""
    if key is None:
        return value
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return ""
    if isinstance(data, dict):
        field = data.get(key)
        if isinstance(field, str) and field:
            return field
    return ""


def _extract_session_id(headers) -> str:
    """Идентификатор сессии агента из входящих заголовков.

    Идёт по списку ``config.ADAPTER_SESSION_HEADER`` и возвращает первое
    непустое значение; ни одного — ``session_log.UNKNOWN_SESSION_ID``.
    Имена сверяются без учёта регистра (``email.message.Message.get``).
    Кандидат вида «Имя:ключ» трактуется как JSON-заголовок: значение
    разбирается как объект и берётся его строковое поле ``ключ`` (Codex CLI
    шлёт ``x-codex-turn-metadata: {"session_id": "...", ...}``).

    Список нужен потому, что агенты называют заголовок по-разному: [CC] CLI
    шлёт ``X-Claude-Code-Session-Id``, а QwenCode по умолчанию id не шлёт
    вовсе и настраивается на произвольное имя через ``customHeaders`` (см.
    docs/environment.md, раздел «Настройка агента (QwenCode)»). Codex CLI шлёт
    id внутри JSON-заголовка ``x-codex-turn-metadata`` — для него кандидат
    задаётся как «Имя:ключ» и читается ``_header_session_value``."""
    for name, key in _session_header_names():
        value = _header_session_value(headers, name, key)
        if value:
            return value
    return UNKNOWN_SESSION_ID


def _register_session(
    session_id: str,
    agent: str,
    *,
    model: str = "",
    backend: str = "",
    route: str = "",
    input: str = "",
) -> None:
    """Учёт обращения в таблице сессий WEBUI (страница "/sessions", v0.9.5;
    v0.9.2 — секция «Sessions» статус-страницы): upsert строки по
    кортежу (session, agent, model, backend, route), calls+1. Ключ строки
    кладётся в thread-local ``_req_ctx.session_key`` — его читает
    ``_note_session_error``, чтобы инкрементить errors именно этой строки.

    Вызывается на КАЖДОМ входном запросе: в 400-ветках валидации тела (до
    ``routing.decide``) — с пустыми model/backend/route; после
    ``routing.decide`` — с реальными (включая reject/disabled: видно, куда
    агент пытался). Смена модели или обработчика даёт НОВУЮ строку (кортеж
    изменился), возврат к прежнему кортежу — ту же (calls+1). Исключений не
    бросает: register сам возвращает None при выключенном учёте.

    ``input`` (v0.9.5) — входной эндпоинт обращения (messages|completions|
    responses): НЕ часть ключа, а сопровождение строки — колонка «Входной
    эндпоинт» и выбор TARGET-селекта строки в таблице Sessions. 400-ветки
    валидации тела передают его, хотя до ``routing.decide`` не дошли: путь
    уже распознан (``routing.input_path_to_format``).

    v0.9.7: помимо ключа строки, здесь же взводится ``_req_ctx.err_eligible``
    — признак «входной путь распознан, ошибка этого запроса подлежит записи в
    .err». Раньше право на .err-блок определялось непустым ``session_key``, и
    при ``ADAPTER_SESSIONS_TABLE=0`` (учёт выключен → ``register`` вернул None)
    ошибки входа молча выпадали из канала. Теперь канал .err от таблицы
    сессий НЕ зависит: счётчик «Ошибок» живёт только при включённой таблице,
    а .err пишется всегда."""
    _req_ctx.err_eligible = True
    _req_ctx.session_key = session_registry.register(
        session_id, agent, model=model, backend=backend, route=route, input=input
    )


def _write_backend_error(session_id, req_id, **kwargs):
    """Записать .err-блок инцидента БЭКЕНДА и пометить запрос как «.err уже
    написан» (v0.9.5, задача 7).

    Обёртка над session_log.write_error_file: подробные ветки do_POST
    (исчерпание ретраев, запрос дошёл до бэкенда) несут ПОЛНОЕ тело запроса к
    бэкенду, поэтому их блок информативнее обобщённого. Флаг
    ``_req_ctx.err_written`` гасит обобщённый блок, который иначе напишет
    ``_flush_pending_error`` в finally, — один запрос = один ERROR-блок."""
    _req_ctx.err_written = True
    write_error_file(session_id, req_id, **kwargs)


def _flush_pending_error() -> None:
    """Дописать .err-блок «ранней» ошибки запроса (v0.9.5, задача 7).

    Точка вызова — ``finally`` в do_POST: к этому моменту известен финальный
    исход запроса. Если ответ клиенту был ошибкой (>= 400) на распознанном
    входном пути (``_req_ctx.err_eligible``), но подробного блока инцидента
    бэкенда не писалось
    (``_write_backend_error``), — пишем обобщённый ERROR-блок
    (``write_session_error``). Так .err покрывает ВЕСЬ ошибочный трафик
    сессии, а не только инциденты бэкенда: 400 валидации тела (Invalid JSON /
    Missing model / strict-модель), 404 disabled-входа, 400/502 reject
    маршрута.

    Запись отложена именно в finally, а не сделана сразу в
    ``_note_session_error``: подробные ветки пишут свой блок ПОСЛЕ отправки
    ответа, и при немедленной записи на один запрос вышло бы два блока.
    Исключений не бросает — наблюдательный канал."""
    pending = getattr(_req_ctx, "error_status", None)
    if not pending or getattr(_req_ctx, "err_written", False):
        return
    status, message = pending
    write_session_error(
        getattr(_req_ctx, "session_id", "") or UNKNOWN_SESSION_ID,
        getattr(_req_ctx, "req_id", "-"),
        final_status=status,
        message=message,
        model=getattr(_req_ctx, "model", ""),
        in_body=getattr(_req_ctx, "in_body", ""),
    )


def _err_ctx_begin(headers, err_eligible: bool = True) -> tuple[str, str]:
    """Инициализировать thread-local контекст запроса для .err-канала.
    Возвращает (session_id, req_id).

    v0.9.7: ``err_eligible`` по умолчанию True — для служебных обработчиков
    (do_GET, неподдерживаемые HTTP-методы), которые по определению отвечают
    ошибкой (501), нужной в .err. В do_POST флаг стартует False и взводится
    в ``_register_session`` (после распознавания входного пути), чтобы 404
    не-входного пути .err не писал."""
    session_id = _extract_session_id(headers)
    req_id = uuid.uuid4().hex[:12]
    _req_ctx.req_id = req_id
    _req_ctx.session_id = session_id
    _req_ctx.session_key = None
    _req_ctx.err_eligible = err_eligible
    _req_ctx.error_status = None
    _req_ctx.err_written = False
    _req_ctx.response_started = False
    _req_ctx.in_body = ""
    _req_ctx.model = ""
    return session_id, req_id


def _err_ctx_end() -> None:
    """Закрыть thread-local контекст запроса: дописать отложенный .err-блок
    (``_flush_pending_error``) и убрать атрибуты, иначе они «залипнут» на
    следующий запрос в этом же потоке (ThreadingHTTPServer переиспользует
    потоки). Парная к ``_err_ctx_begin``; в do_POST тот же финал выполняется
    в его ``finally`` (там сначала фиксируется usage-учёт)."""
    _flush_pending_error()
    for attr in (
        "req_id",
        "session_id",
        "session_key",
        "err_eligible",
        "error_status",
        "err_written",
        "response_started",
        "in_body",
        "model",
    ):
        if hasattr(_req_ctx, attr):
            delattr(_req_ctx, attr)


class Adapter(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        rid = getattr(_req_ctx, "req_id", "-")
        _d(f"[{rid}] [HTTP] {fmt % args}")

    def _note_session_error(self, status, message=""):
        """Учёт ошибки строки-кортежа таблицы сессий (страница "/sessions"): любой
        финальный HTTP-ответ >= 400. Вызывается из общих методов отправки
        (_send_json/_send_raw), поэтому покрывает все пути ошибок. Ключ берётся
        из ``_req_ctx.session_key`` (ставится ``_register_session``) — ошибка
        ложится в ту же строку, что и обращение. Для запросов без учёта (404
        не-входного пути, GET /api/*, health) ключа нет → record_error(None) —
        no-op.

        v0.9.5 (задача 7): здесь же запоминается ПЕРВАЯ ошибка запроса
        (``_req_ctx.error_status``) — .err-блок по ней допишет
        ``_flush_pending_error`` в finally. Отложенность нужна, чтобы
        подробный блок инцидента бэкенда (пишется после отправки ответа) не
        дублировался обобщённым.

        v0.9.7: право на .err-блок определяется ``_req_ctx.err_eligible``
        (входной путь распознан / служебный обработчик), а НЕ непустым
        ``session_key`` — так ошибки пишутся в .err и при выключенной таблице
        сессий (``ADAPTER_SESSIONS_TABLE=0``). Счётчик строки по-прежнему
        инкрементится только при живом ключе (``record_error``)."""
        if status < 400:
            return
        key = getattr(_req_ctx, "session_key", None)
        session_registry.record_error(key)
        if getattr(_req_ctx, "err_eligible", False) and (
            getattr(_req_ctx, "error_status", None) is None
        ):
            _req_ctx.error_status = (status, message)

    def _send_json(self, status, data):
        # message — текст ошибки для .err-блока (v0.9.5): у всех ошибок
        # адаптера ответ имеет вид {"error": "..."} — берём его оттуда.
        message = ""
        if isinstance(data, dict):
            message = str(data.get("error", "") or "")
        self._note_session_error(status, message)
        _req_ctx.response_started = True
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
        self._note_session_error(status)
        _req_ctx.response_started = True
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
        # Ответ уже начат: верхнеуровневый except в do_POST не сможет отдать
        # JSON-error статус (см. _req_ctx.response_started).
        _req_ctx.response_started = True

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
        # Контекст .err-канала заводим на весь обработчик (v0.9.7): /v1/models
        # отвечает 501, пока список моделей не прогрет, — эту ошибку нужно
        # видеть в .err. Прочие GET-пути (health-check / 404) .err не пишут,
        # поэтому err_eligible стартует False и взводится только для /v1/models.
        _err_ctx_begin(self.headers, err_eligible=False)
        try:
            if self.path == "/v1/models":
                # Возвращаем список моделей, полученный от бэкенда.
                # Формат — [OI]-совместимый: {object: "list", data: [...]}.
                _req_ctx.err_eligible = True
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
        finally:
            _err_ctx_end()

    def _unsupported_method(self):
        """Неподдерживаемый HTTP-метод (PUT/DELETE/PATCH/...): базовый
        BaseHTTPRequestHandler отвечает на них 501 через send_error()
        (HTML-тело, мимо _send_json) — такая ошибка в .err не попадала.
        Отвечаем JSON-ом и проводим её через .err-канал (v0.9.7). Контекст
        запроса заводим явно — do_POST здесь не вызывается."""
        _err_ctx_begin(self.headers)
        try:
            self._send_json(501, {"error": f"Unsupported method: {self.command}"})
        finally:
            _err_ctx_end()

    # Не-POST/GET/HEAD методы уходят в один обработчик (JSON-501 + .err).
    def do_PUT(self):
        self._unsupported_method()

    def do_DELETE(self):
        self._unsupported_method()

    def do_PATCH(self):
        self._unsupported_method()

    def do_OPTIONS(self):
        self._unsupported_method()

    def do_TRACE(self):
        self._unsupported_method()

    def do_POST(self):
        req_t0 = time.time()
        # Контекст .err-канала запроса (v0.9.7) заводится общим хелпером:
        # error_status — первая ошибка ответа (status, message) для
        # обобщённого блока, err_written — «подробный блок инцидента бэкенда
        # уже написан». Сброс на КАЖДОМ запросе обязателен: thread-local
        # переживает запрос в пределах потока, и «залипшие» значения исказили
        # бы следующий запрос (пропущенный или лишний .err-блок).
        # err_eligible стартует False — право на .err даёт распознанный
        # входной путь (взводится в _register_session); in_body/model — тело
        # и модель запроса для «ранних» ошибок (заполняются по ходу разбора).
        session_id, req_id = _err_ctx_begin(self.headers, err_eligible=False)
        # Имя агента для таблицы сессий WEBUI (страница "/sessions", v0.9.5):
        # User-Agent до
        # первого пробела — [CC] CLI шлёт «claude-cli/2.1.236 (external, cli)»,
        # в колонку «Агент» идёт «claude-cli/2.1.236». Заголовка может не быть
        # (иные клиенты) — тогда пустая строка.
        agent = self.headers.get("User-Agent", "").split(" ", 1)[0]

        # Учёт usage (таблица WEBUI «Использованные модели», колонки
        # Input/Output): локальные аккумуляторы токенов из usage-блоков
        # ответов бэкенда на время запроса, фиксация — в finally (см. внизу
        # do_POST). Активен только для запросов, прошедших strict-проверку
        # (usage_active ставится после record_model_usage) — 400-пути в
        # счётчики не попадают.
        usage_tokens = {"input": 0, "output": 0}
        usage_active = False

        # Ключ строки таблицы сессий (страница "/sessions") ставится
        # _register_session — на входном пути (после распознавания пути /
        # после routing.decide). None — «учёта нет»: счётчик строки не
        # инкрементится, но .err-канал от ключа не зависит (v0.9.7).
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

            # Учёт сессии для таблицы сессий WEBUI (страница "/sessions",
            # v0.9.5): строка —
            # кортеж (session, agent, model, backend, route), её создаёт
            # _register_session. Здесь, до чтения тела, вызова нет: строку
            # поставят либо 400-ветки ниже (пустым кортежем), либо ветка после
            # routing.decide (полным). 404 на не-входной путь строку НЕ создаёт
            # (учёт стоит после return).

            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            _body = body.decode()
            # Тело запроса агента — для .err-блока «ранних» ошибок адаптера
            # (v0.9.5, задача 7): _note_session_error пишет [REQUEST] из него,
            # когда запрос до бэкенда не дошёл и out_body не сформирован.
            _req_ctx.in_body = _body
            # Полная строка: консоль обрежет её до ADAPTER_DEBUG_TRIM в
            # logger._write, файл при ADAPTER_DEBUG_ENABLE=1 получит полную.
            _dr(req_id, f"[BODY] {_body}")
            if config.ADAPTER_DEBUG_PARTS:
                write_debug_json(session_id, "BODY", _body)

            try:
                anthropic_req = json.loads(body)
            except json.JSONDecodeError as e:
                # Обращение учтено пустым кортежем: до routing.decide не дошли.
                _register_session(session_id, agent, input=inp_fmt)
                self._send_json(400, {"error": f"Invalid JSON: {e}"})
                return

            # === /model <имя> — служебная команда переключения модели (v0.9.6) ===
            # Работает для ЛЮБОГО TARGET входа responses, который идёт через
            # конвертор, а не дословно — сейчас это "responses" (внутренний
            # конвертор, см. IMPLEMENTED_CONVERSIONS[("responses","responses")])
            # и "completions" (полная кросс-форматная конвертация, см.
            # IMPLEMENTED_CONVERSIONS[("responses","completions")], v0.9.7); НЕ
            # срабатывает при TARGET=passthrough (там тело уходит дословно,
            # без вмешательств, по определению). Нативное /model самого Codex
            # CLI на кастомном model_provider ненадёжно (баги пикера моделей и
            # клонирования контекста провайдера при смене модели без рестарта
            # сессии — см. документацию проекта), поэтому адаптер держит
            # собственный канал: пользователь пишет "/model <имя>" обычным
            # сообщением, detect_model_switch_command перехватывает его ЗДЕСЬ,
            # до похода к бэкенду; выбор сохраняется в session_settings и
            # действует для всех последующих запросов сессии, пока не будет
            # переключён снова. Ответ-подтверждение — всегда в формате
            # Responses (клиент говорит с адаптером через /v1/responses
            # независимо от TARGET-а, которым обслуживается сам обмен).
            if inp_fmt == "responses" and routing.target_for_input(inp_fmt, session_id) in (
                "responses",
                "completions",
            ):
                switch_to = detect_model_switch_command(anthropic_req.get("input") or [])
                if switch_to:
                    session_settings.set_model_override(session_id, switch_to)
                    _dr(req_id, f"[MODEL_SWITCH] session={session_id} -> {switch_to}")
                    _trace(session_id, req_id, "model_switch", model=switch_to)
                    _register_session(
                        session_id, agent, model=switch_to, route="model_switch", input=inp_fmt
                    )
                    ack_text = f"Model switched to `{switch_to}`."
                    if bool(anthropic_req.get("stream", True)):
                        self._start_sse(200)
                        emit_responses_control_message(self.wfile, ack_text, switch_to)
                    else:
                        self._send_json(200, build_responses_control_response(ack_text, switch_to))
                    return
                override_model = session_settings.model_override(session_id)
                if override_model:
                    _dr(
                        req_id,
                        f"[MODEL_OVERRIDE] session={session_id}: клиент прислал model="
                        f"{anthropic_req.get('model')!r}, применяю оверрайд сессии -> {override_model}",
                    )
                    anthropic_req["model"] = override_model

            model = anthropic_req.get("model")
            if not model:
                _register_session(session_id, agent, input=inp_fmt)
                self._send_json(400, {"error": "Missing required field: model"})
                return

            # Валидация имени модели (до маппинга и разрешения бэкенда):
            # имя из запроса клиента — именно оно указано в _AVAILABLE_MODELS
            # (с возможными префиксами бэкендов для коллизирующих моделей).
            client_model = model
            # Модель — в _req_ctx для .err-блока «ранних» ошибок (v0.9.5):
            # к моменту 400-веток ниже она уже известна.
            _req_ctx.model = client_model
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
                # Обращение учтено пустым кортежем: до routing.decide не дошли.
                _register_session(session_id, agent, input=inp_fmt)
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
            #   reject   — маршрута нет (нереализованная пара / бэкенд
            #              подтверждённо не поддерживает целевой формат) →
            #              400|502, тоже до учёта;
            #   passthrough — TARGET=passthrough: тело уходит бэкенду КАК ЕСТЬ
            #              на эндпойнт входного формата (E→E);
            #   convert  — прямое преобразование входа в формат-цель:
            #              messages→completions (полный конвертер) или
            #              messages→messages (сортировка system в начало).
            # v0.9.5: session_id передаётся в decide/target_for_input — TARGET
            # может быть переопределён ДЛЯ СЕССИИ (session_settings), не трогая
            # общую настройку приложения.
            route = routing.decide(inp_fmt, backend_name, session_id)
            route_action, out_fmt, route_msg, route_status = route
            _dr(
                req_id,
                f"[ROUTE] input={inp_fmt} backend={backend_name} "
                f"target={routing.target_for_input(inp_fmt, session_id)} -> "
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
                target=routing.target_for_input(inp_fmt, session_id),
                action=route_action,
                output=out_fmt,
                error=route_msg or None,
                http_status=route_status,
            )
            # Учёт сессии (таблица сессий WEBUI, страница "/sessions"): строка — кортеж
            # (session, agent, model, backend, route). Ставится ДО ветки
            # disabled/reject — таблица показывает, куда агент пытался (в т.ч.
            # «reject»/«disabled»); client_model — клиентское имя до маппинга,
            # как в таблице «Models in use». Для passthrough out_fmt == вход →
            # «passthrough X→X»; для convert — «convert X→Y» (в т.ч.
            # «convert messages→messages»). Смена модели или обработчика даёт
            # НОВУЮ строку; повторный запрос тем же кортежем — ту же (calls+1).
            if route_action == "passthrough":
                route_str = f"passthrough {inp_fmt}→{inp_fmt}"
            elif route_action == "convert":
                route_str = f"convert {inp_fmt}→{out_fmt}"
            else:
                route_str = route_action
            _register_session(
                session_id,
                agent,
                model=client_model,
                backend=backend_name,
                route=route_str,
                input=inp_fmt,
            )
            if route_action in ("disabled", "reject"):
                _dr(req_id, f"[ROUTE_REJECT] {route_msg}")
                self._send_json(route_status, {"error": route_msg})
                return

            # Маршрут определён: format'ы исходящего запроса (convert → out,
            # passthrough → сам вход) и признак «цель == вход» (verbatim):
            # тело НЕ пересобирается в [OI]-формат, а уходит на эндпойнт
            # выходного формата как есть — см. ветку ниже. Верно и для
            # TARGET=passthrough, и для convert messages→messages (там лишь
            # дополнительно сортируются system-сообщения).
            if route_action == "convert":
                assert out_fmt is not None
                out_fmt_val = out_fmt
            else:
                out_fmt_val = inp_fmt
            verbatim = out_fmt_val == inp_fmt
            # Признак «именно TARGET=passthrough» (для trace/логов): convert
            # messages→messages тоже идёт дословной веткой, но это конверсия.
            is_passthrough = route_action == "passthrough"

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

            # === Дословная передача (E→E): TARGET=passthrough или convert
            #     messages→messages (сортировка system) / responses→responses
            #     (store=false + /model, v0.9.6) ===
            # Тело запроса уходит бэкенду КАК ПРИШЛО — единственные мутации:
            # ``model`` заменяется на resolved_model (маппинг/префикс бэкенда
            # применяются и здесь), при аварийном рубильнике
            # ADAPTER_STREAMING_ENABLE=0 ``stream`` принудительно гасится, а для
            # out_fmt_val == "responses" ещё и store принудительно false
            # (force_store_false) — /model-команда обрабатывается раньше, до
            # этой ветки (см. блок в начале do_POST), сюда доходят уже обычные
            # запросы. Конвертация, strict-поля messages (max_tokens/system/
            # tools/…) и антропик-трассировка сюда не входят: контракт формата
            # E→E определяет бэкенд (он сам ответит 400 на невалидное тело), а
            # не адаптер. Ответ бэкенда отдаётся клиенту ДОСЛОВНО (см.
            # passthrough-хвост ниже) — usage читается по формату ``out_fmt``.
            if verbatim:
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
                # store=false — принудительно для /v1/responses (v0.9.6), и в
                # TARGET=passthrough, и в TARGET=responses/внутреннем конверторе:
                # previous_response_id может резолвить только бэкенд, который сам
                # создал предыдущий response, а сторонний бэкенд за этим
                # адаптером чужих ответов не хранит. Пер-сессионный разбор
                # реального трафика Codex CLI показал, что клиент уже шлёт
                # store:false сам и пересобирает контекст на своей стороне (см.
                # документацию проекта) — но это инвариант эндпоинта, а не
                # свойство одного наблюдённого клиента, поэтому приводим
                # принудительно (force_store_false, convert.py), а не полагаемся
                # на добросовестность отправителя.
                if out_fmt_val == "responses" and force_store_false(anthropic_req):
                    _dr(req_id, "[STORE_ENFORCED] responses: store forced to false")
                # convert messages→messages (v0.9.2): бэкенд (vLLM-шаблон
                # чата) требует system первым (Jinja raise_exception 'System
                # message must be at the beginning'), а [CC]-сессии несут
                # system-сообщения по ходу диалога и <system-reminder> внутри
                # user. Переносим все role=system в начало (в исходном
                # порядке, БЕЗ склейки) — сообщения не пересобираются
                # (та же идея, что в convert-ветке, где system собираются в
                # начало; там — склейкой). Это ЕДИНСТВЕННОЕ отличие от
                # TARGET=passthrough, который уходит дословно, без
                # перестановки: passthrough — «передать без преобразования»,
                # а TARGET=messages — «преобразование messages→messages»
                # (сортировка полей). Прочие дословные пути
                # (completions→completions, responses→responses) уходят как
                # есть: там нет инварианта «system первым» (responses→responses
                # с v0.9.6 всё же не полностью пассивен — см. блок
                # STORE_ENFORCED/MODEL_SWITCH выше и в начале do_POST, но это
                # НЕ перестановка полей, а два точечных вмешательства).
                if (
                    route_action == "convert"
                    and inp_fmt == "messages"
                    and out_fmt_val == "messages"
                ):
                    original_msgs = anthropic_req.get("messages", [])
                    reordered = normalize_messages_system_first(original_msgs)
                    if reordered != original_msgs:
                        anthropic_req["messages"] = reordered
                        _dr(req_id, "[SYSTEM_FIRST] messages→messages: system перенесены в начало")
                _trace(
                    session_id,
                    req_id,
                    "request_start",
                    path=self.path,
                    model=model,
                    input_format=inp_fmt,
                    passthrough=is_passthrough,
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
                                passthrough=is_passthrough,
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
                                passthrough=is_passthrough,
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
                                passthrough=is_passthrough,
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
                                passthrough=is_passthrough,
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
                                    passthrough=is_passthrough,
                                )
                                # Обрыв потока — инцидент (v0.9.7): заголовки 200
                                # уже ушли клиенту, но ответ оборван. Пишем .err
                                # (final_status=200 + пометка mid-stream abort) и
                                # инкрементим счётчик «Ошибок» строки сессии.
                                _write_backend_error(
                                    session_id,
                                    req_id,
                                    final_status=200,
                                    backend_url=backend_url,
                                    model=model,
                                    out_body=out_body,
                                    err_body=f"mid-stream abort: {type(e).__name__}: {e}",
                                )
                                session_registry.record_error(
                                    getattr(_req_ctx, "session_key", None)
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
                            passthrough=is_passthrough,
                        )
                        # Протокол .err-инцидентов: финальный ответ — ошибка
                        # 4xx/5xx, запрос дошёл до бэкенда (out_body
                        # сформирован). Файл пишется БЕЗУСЛОВНО и помечается
                        # err_written — _note_session_error второго блока не
                        # добавит.
                        _write_backend_error(
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
                            passthrough=is_passthrough,
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
                            passthrough=is_passthrough,
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
                            passthrough=is_passthrough,
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
                        passthrough=is_passthrough,
                    )
                    _write_backend_error(
                        session_id,
                        req_id,
                        final_status=final_status,
                        backend_url=backend_url,
                        model=model,
                        out_body=out_body,
                        err_body=msg,
                    )
                return

            # === responses → completions (v0.9.7) ===
            # Полная кросс-форматная конвертация: TARGET=completions для входа
            # responses (routing.IMPLEMENTED_CONVERSIONS[("responses",
            # "completions")]). До этой точки дошли, только если verbatim=False
            # (иначе выше уже был return) — при текущем реестре это означает
            # РОВНО inp_fmt=="responses" (единственная незеркальная пара для
            # входа responses, кроме уже отработавшего self-пары выше).
            # messages→completions ниже — отдельная, НЕ общая с этой веткой
            # реализация (см. её собственный комментарий): дублирование
            # структуры retry-цикла осознанное и следует уже принятому в
            # проекте разделению (у passthrough-стрима и messages→completions-
            # стрима тоже два независимых цикла, а не общий helper).
            if inp_fmt == "responses":
                assert out_fmt_val == "completions", out_fmt_val

                r_stream_requested = bool(anthropic_req.get("stream", False))
                if r_stream_requested and not config.ADAPTER_STREAMING_ENABLE:
                    _dr(
                        req_id,
                        "[STREAM_DISABLED] Клиент просил stream=true, но "
                        "ADAPTER_STREAMING_ENABLE=0 -> forcing backend_stream=False",
                    )
                    r_stream_requested = False

                r_instructions = anthropic_req.get("instructions")
                r_input = anthropic_req.get("input") or []
                r_messages = convert_responses_input_to_openai_messages(r_input, r_instructions)

                # Трассировка tool_result — ДО отправки бэкенду, наблюдение за
                # тем, что харнесс уже прислал в этом запросе (см. docstring
                # extract_responses_tool_results — без кросс-запросной
                # привязки к породившему tool-call, в отличие от Anthropic-пути).
                for tr in extract_responses_tool_results(r_input):
                    _dr(
                        req_id,
                        f"[TOOL_RESULT] call_id={tr['call_id']} len={len(str(tr['content']))}",
                    )
                    _trace(
                        session_id,
                        req_id,
                        "tool_result",
                        tool_use_id=tr["call_id"],
                        content=tr["content"],
                    )

                r_openai_body: dict = {
                    "model": model,
                    "messages": r_messages,
                    "stream": r_stream_requested,
                }
                if r_stream_requested and config.ADAPTER_STREAM_INCLUDE_USAGE:
                    r_openai_body["stream_options"] = {"include_usage": True}
                if anthropic_req.get("tools"):
                    r_openai_body["tools"] = convert_responses_tools_to_openai(
                        anthropic_req["tools"]
                    )
                    _dr(req_id, f"[TOOLS] Passed {len(r_openai_body['tools'])} tools")
                if anthropic_req.get("tool_choice"):
                    r_openai_body["tool_choice"] = convert_responses_tool_choice_to_openai(
                        anthropic_req["tool_choice"]
                    )
                    _dr(req_id, f"[TOOL_CHOICE] {r_openai_body['tool_choice']}")

                # Тот же инвариант конвертации, что и у messages→completions
                # ниже: часть бэкендов (vLLM-шаблон чата) требует system
                # первым сообщением. Здесь он обеспечен
                # normalize_messages_system_first ВНУТРИ
                # convert_responses_input_to_openai_messages — проверка тут
                # только диагностическая, как и в messages→completions.
                r_system_ok = bool(r_messages) and r_messages[0]["role"] == "system"
                _trace(
                    session_id,
                    req_id,
                    "adapter_invariant_check",
                    check="system_message_first",
                    passed=r_system_ok,
                    first_role=(r_messages[0]["role"] if r_messages else None),
                )
                _dr(
                    req_id,
                    "[CHECK] First message is system, OK"
                    if r_system_ok
                    else f"[WARN] First message is NOT system: "
                    f"{r_messages[0]['role'] if r_messages else 'empty'}",
                )

                _dr(req_id, f"[OPENAI_BODY] {json.dumps(r_openai_body, ensure_ascii=False)}")
                if config.ADAPTER_DEBUG_PARTS:
                    write_debug_json(session_id, "OPENAI_BODY", r_openai_body)

                r_backend_url = backend_cfg["base"].rstrip("/") + "/v1/chat/completions"
                r_backend_key = backend_cfg["key"]
                r_out_body = json.dumps(r_openai_body, ensure_ascii=False).encode()
                r_req = urllib.request.Request(
                    r_backend_url,
                    data=r_out_body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {r_backend_key}",
                        "Connection": "keep-alive",
                    },
                    method="POST",
                )

                if r_stream_requested:
                    r_last_error: tuple[int | str, str] | None = None
                    r_started = False
                    for attempt in range(1, ADAPTER_RETRY + 1):
                        try:
                            _dr(
                                req_id,
                                f"[FETCH] (responses->completions stream) Attempt "
                                f"{attempt}/{ADAPTER_RETRY}, timeout={ADAPTER_TIMEOUT}s",
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
                            resp = urllib.request.urlopen(
                                r_req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT
                            )
                            _dr(
                                req_id,
                                f"[FETCH] Заголовки получены за {time.time() - t0:.1f}s, "
                                f"status={resp.status}",
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
                            r_started = True
                            r_approx_prompt_chars = len(json.dumps(r_messages, ensure_ascii=False))
                            _finish_reason, r_usage = stream_openai_completions_to_responses(
                                resp,
                                self.wfile,
                                model,
                                session_id,
                                req_id,
                                approx_prompt_chars=r_approx_prompt_chars,
                                out_body=r_out_body,
                                backend_url=r_backend_url,
                            )
                            if r_usage.get("prompt_tokens") or r_usage.get("completion_tokens"):
                                usage_tokens["input"] += int(r_usage.get("prompt_tokens") or 0)
                                usage_tokens["output"] += int(r_usage.get("completion_tokens") or 0)
                            _dr(req_id, f"[OK] Stream done, finish_reason={_finish_reason}")
                            _trace(
                                session_id,
                                req_id,
                                "request_end",
                                http_status=200,
                                retries_used=attempt - 1,
                                total_elapsed_ms=int((time.time() - req_t0) * 1000),
                                streamed=True,
                            )
                            return

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
                            r_last_error = (e.code, err)
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
                            r_last_error = ("timeout", str(e))
                            if attempt < ADAPTER_RETRY:
                                delay = 2**attempt
                                _dr(req_id, f"[RETRY] Waiting {delay}s before next attempt...")
                                time.sleep(delay)

                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
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
                            r_last_error = ("error", str(e))
                            if r_started:
                                with contextlib.suppress(Exception):
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
                                )
                                # Обрыв потока — инцидент (v0.9.7): см.
                                # аналогичную ветку passthrough.
                                _write_backend_error(
                                    session_id,
                                    req_id,
                                    final_status=200,
                                    backend_url=r_backend_url,
                                    model=model,
                                    out_body=r_out_body,
                                    err_body=f"mid-stream abort: {type(e).__name__}: {e}",
                                )
                                session_registry.record_error(
                                    getattr(_req_ctx, "session_key", None)
                                )
                                return
                            if attempt < ADAPTER_RETRY:
                                delay = 2**attempt
                                _dr(req_id, f"[RETRY] Waiting {delay}s...")
                                time.sleep(delay)

                    if r_last_error and not r_started:
                        code, msg = r_last_error
                        if code == "timeout":
                            self._send_json(
                                504,
                                {"error": f"Gateway timeout after {ADAPTER_RETRY} attempts: {msg}"},
                            )
                            final_status = 504
                        elif isinstance(code, int):
                            self._send_json(code, {"error": f"Backend error: {msg}"})
                            final_status = code
                        else:
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
                        )
                        _write_backend_error(
                            session_id,
                            req_id,
                            final_status=final_status,
                            backend_url=r_backend_url,
                            model=model,
                            out_body=r_out_body,
                            err_body=msg,
                        )
                    return

                # === Нестриминговая ветка responses→completions ===
                r_last_error = None
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
                        resp = urllib.request.urlopen(
                            r_req, context=SSL_CTX, timeout=ADAPTER_TIMEOUT
                        )
                        raw = resp.read()
                        elapsed = time.time() - t0
                        _dr(
                            req_id,
                            f"[FETCH] Success in {elapsed:.1f}s, {resp.status}, {len(raw)} bytes",
                        )
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
                        responses_resp = convert_openai_completions_to_responses(o, model)
                        u = o.get("usage") or {}
                        if u.get("prompt_tokens") or u.get("completion_tokens"):
                            usage_tokens["input"] += int(u.get("prompt_tokens") or 0)
                            usage_tokens["output"] += int(u.get("completion_tokens") or 0)
                        _dr(req_id, f"[RESPONSE] {json.dumps(responses_resp, ensure_ascii=False)}")
                        if config.ADAPTER_DEBUG_PARTS:
                            write_debug_json(session_id, "RESPONSE", responses_resp)
                        self._send_json(200, responses_resp)
                        _dr(req_id, "[OK] Done")
                        _trace(
                            session_id,
                            req_id,
                            "request_end",
                            http_status=200,
                            retries_used=attempt - 1,
                            total_elapsed_ms=int((time.time() - req_t0) * 1000),
                            streamed=False,
                        )
                        return
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
                        r_last_error = (e.code, err)
                        if e.code not in (429, 502, 503, 504):
                            break
                        if attempt < ADAPTER_RETRY:
                            time.sleep(2**attempt)
                    except TimeoutError as e:
                        _dr(req_id, f"[TIMEOUT] Attempt {attempt}/{ADAPTER_RETRY} timed out")
                        _trace(
                            session_id,
                            req_id,
                            "backend_result",
                            attempt=attempt,
                            ok=False,
                            status="timeout",
                            error=str(e),
                        )
                        r_last_error = ("timeout", str(e))
                        if attempt < ADAPTER_RETRY:
                            time.sleep(2**attempt)
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
                        r_last_error = ("error", str(e))
                        if attempt < ADAPTER_RETRY:
                            time.sleep(2**attempt)

                # Все попытки исчерпаны — как соседние ветки: 504/код/502.
                # r_last_error может остаться None при ADAPTER_RETRY_COUNT=0
                # (цикл не выполнился) — fallback 502, как в convert-ветке ниже.
                if r_last_error:
                    code, msg = r_last_error
                    if code == "timeout":
                        self._send_json(
                            504, {"error": f"Gateway timeout after {ADAPTER_RETRY} attempts: {msg}"}
                        )
                        final_status = 504
                    elif isinstance(code, int):
                        self._send_json(code, {"error": f"Backend error: {msg}"})
                        final_status = code
                    else:
                        self._send_json(
                            502,
                            {"error": f"Backend unavailable after {ADAPTER_RETRY} attempts: {msg}"},
                        )
                        final_status = 502
                else:
                    msg = f"Backend unavailable after {ADAPTER_RETRY} attempts"
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
                    streamed=False,
                )
                _write_backend_error(
                    session_id,
                    req_id,
                    final_status=final_status,
                    backend_url=r_backend_url,
                    model=model,
                    out_body=r_out_body,
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
                            # Обрыв потока — инцидент (v0.9.7): см.
                            # аналогичную ветку passthrough.
                            _write_backend_error(
                                session_id,
                                req_id,
                                final_status=200,
                                backend_url=backend_url,
                                model=model,
                                out_body=out_body,
                                err_body=f"mid-stream abort: {type(e).__name__}: {e}",
                            )
                            session_registry.record_error(getattr(_req_ctx, "session_key", None))
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
                    _write_backend_error(
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
            _write_backend_error(
                session_id,
                req_id,
                final_status=final_status,
                backend_url=backend_url,
                model=model,
                out_body=out_body,
                err_body=msg,
            )
        except Exception as e:
            # Верхнеуровневый перехват (v0.9.7): раньше исключение из тела
            # do_POST уходило в socketserver.handle_error — клиент не получал
            # ответа вовсе, а .err-канал молчал. Теперь отвечаем 500 (если
            # ответ ещё не начат) и всегда фиксируем инцидент.
            _dr(req_id, f"[FAIL] Unhandled {type(e).__name__}: {e}")
            if not getattr(_req_ctx, "response_started", False):
                # Ответ не начат — можно отдать честный 500; это же выставит
                # error_status, и finally допишет обобщённый .err-блок.
                self._send_json(500, {"error": f"Internal adapter error: {type(e).__name__}: {e}"})
            else:
                # Стрим уже начат (заголовки ушли): статус клиенту не
                # поменять. Инцидент всё равно фиксируем в .err — помечаем
                # финальный статус 200 с текстом обрыва.
                _req_ctx.error_status = (
                    200,
                    f"unhandled mid-stream error: {type(e).__name__}: {e}",
                )
            _trace(
                session_id,
                req_id,
                "request_end",
                failed=True,
                error=f"{type(e).__name__}: {e}",
                total_elapsed_ms=int((time.time() - req_t0) * 1000),
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
            # .err-блок «ранней» ошибки запроса (v0.9.5, задача 7) и очистка
            # thread-local — общий финал (v0.9.7), см. _flush_pending_error /
            # _err_ctx_end.
            _err_ctx_end()
