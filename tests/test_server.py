"""Integration tests for backend_adapter.server — HTTP server with fake backend."""
import json
import os
import socket
import sys
import threading
import time


def _send_http(host, port, method, path, body=None, headers=None, timeout=3):
    """Send HTTP request and return decoded response."""
    sock = socket.create_connection((host, port), timeout=5)
    try:
        request = f"{method} {path} HTTP/1.0\r\n"
        request += f"Host: localhost\r\n"
        if headers:
            for k, v in headers.items():
                request += f"{k}: {v}\r\n"
        if body is not None:
            body_bytes = json.dumps(body).encode()
            request += f"Content-Type: application/json\r\n"
            request += f"Content-Length: {len(body_bytes)}\r\n"
            request += "\r\n"
            request += body_bytes.decode()
        else:
            request += "\r\n"
        sock.sendall(request.encode())
        response = b""
        while True:
            try:
                sock.settimeout(timeout)
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            except socket.timeout:
                break
        response_text = response.decode()
        parts = response_text.split("\r\n", 1)
        if not parts[0].strip():
            return {"status": 0, "body": ""}
        status_code = int(parts[0].split(" ")[1])
        parts2 = response_text.split("\r\n\r\n", 1)
        body_part = parts2[1] if len(parts2) > 1 else ""
        return {"status": status_code, "body": body_part}
    finally:
        sock.close()


class ServerSetupMixin:
    """Настройка адаптера поверх fake-бэкенда.

    Общий для TestServer и TestErrFileProtocol. Отдельный класс (не
    TestServer-наследование), чтобы тесты TestErrFileProtocol не тянули
    унаследованные тесты родителя.
    """

    def _setup_adapter(self, fake_backend, mock_logger=True, mock_err=True):
        """Set up adapter pointing at fake backend (single-backend YAML config).

        Uses direct attribute patching on already-loaded modules to avoid
        circular import issues from module deletion + reimport. The backend
        config is a one-entry multi-backend structure — единственный режим
        конфигурации бэкендов.

        mock_logger=False оставляет реальные logger._d/_dr (нужно тестам
        консольного вывода — они проверяют печать через capsys).
        """
        from backend_adapter import config, server as server_mod
        from backend_adapter import session_log as session_log_mod

        cfg = {"name": "test", "base": fake_backend.base_url, "key": "test-key"}
        config._BACKENDS = [cfg]
        config._BACKEND_BY_NAME = {"test": cfg}
        config._MODEL_TO_BACKEND = {"test-model": ("test", cfg)}
        config._DEFAULT_BACKEND = cfg
        config._AVAILABLE_MODELS["test-model"] = {"id": "test-model"}

        # Patch server's logger helpers to avoid file I/O blocking
        if mock_logger:
            server_mod._d = lambda *a, **kw: None
            server_mod._dr = lambda *a, **kw: None
        server_mod._trace = lambda *a, **kw: None
        server_mod.write_debug_json = lambda *a, **kw: None
        # .err-канал (v0.9.0 инциденты + v0.9.1 WARN) безусловен (не зависит от
        # ADAPTER_DEBUG_ENABLE) — в unit-контексте без LOGPATH его надо мокать,
        # иначе записи уходят в дефолтный ./tmp/logs. Тесты самого .err ставят
        # LOGPATH на tmp_path и вызывают _setup_adapter(mock_err=False), чтобы
        # канал был живым (в т.ч. WARN «First message is NOT system»).
        if mock_err:
            server_mod.write_error_file = lambda *a, **kw: None
            server_mod.write_warn_file = lambda *a, **kw: None

        # Used-models table: reset + mock the per-model endpoint probe so the
        # server hook never fires real network requests; tests override the
        # fake to assert probe counts.
        from backend_adapter import model_usage as model_usage_mod
        model_usage_mod.reset_model_usage()
        model_usage_mod._probe_model_endpoints = (
            lambda backend, model: {"endpoints": {}, "errors": {}}
        )

        # Disable SSL so the adapter can connect to the plain-HTTP fake backend.
        # `server.py` does `from .config import SSL_CTX` — patch both the
        # captured reference and the global SSL_CTX in config.
        import ssl as ssl_mod
        server_mod.SSL_CTX = None
        config.SSL_CTX = None

        # Patch urllib.request.urlopen globally to strip SSL context.
        # The adapter calls urllib.request.urlopen(req, context=SSL_CTX, ...)
        # but the fake backend is plain HTTP. We intercept and remove context
        # so urllib uses plain HTTP instead of attempting an SSL handshake.
        import urllib.request as req_mod
        orig_urlopen = req_mod.urlopen
        server_mod._orig_urlopen = orig_urlopen  # save for cleanup
        def no_ssl_urlopen(url, data=None, context=None, *args, **kwargs):
            return orig_urlopen(url, data=data, context=None, *args, **kwargs)
        req_mod.urlopen = no_ssl_urlopen

        from backend_adapter.server import QuietThreadingHTTPServer, Adapter
        server = QuietThreadingHTTPServer(("127.0.0.1", 0), Adapter)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        server.port = server.server_address[1]
        # Wait for server to actually accept connections
        time.sleep(0.3)
        return server


class TestServer(ServerSetupMixin):
    """Integration tests for the HTTP server."""

    def test_full_message_flow(self, fake_backend):
        """Full POST /v1/messages cycle: convert + response."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat123",
            "model": "test-model",
            "choices": [{
                "message": {"role": "assistant", "content": "Hello world"},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={
                                      "model": "test-model",
                                      "messages": [{"role": "user", "content": "Hi"}],
                                      "max_tokens": 100,
                                  })
                assert resp["status"] == 200
                data = json.loads(resp["body"])
                assert data["role"] == "assistant"
                assert data["type"] == "message"
                assert any("Hello world" in str(c) for c in data.get("content", []))
            finally:
                server.shutdown()

    def test_strict_models_blocks_unknown(self, fake_backend):
        """ADAPTER_STRICT_MODELS should reject unknown models."""
        fake_backend.models_response = {"data": [{"id": "known-model"}]}
        fake_backend.completions_response = {}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={
                                      "model": "unknown-model",
                                      "messages": [{"role": "user", "content": "Hi"}],
                                      "max_tokens": 100,
                                  })
                assert resp["status"] == 400
            finally:
                server.shutdown()

    def test_get_models(self, fake_backend):
        """GET /v1/models should return available models."""
        fake_backend.models_response = {"data": [{"id": "m1"}, {"id": "m2"}]}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            # Add extra model to _AVAILABLE_MODELS to simulate model probing
            from backend_adapter import config as cfg
            cfg._AVAILABLE_MODELS["m1"] = {"id": "m1"}
            cfg._AVAILABLE_MODELS["m2"] = {"id": "m2"}
            try:
                resp = _send_http("127.0.0.1", server.port, "GET", "/v1/models")
                assert resp["status"] == 200
                data = json.loads(resp["body"])
                assert data["object"] == "list"
                assert len(data["data"]) == 3  # test-model + m1 + m2
            finally:
                server.shutdown()

    def test_head_request(self, fake_backend):
        """HEAD request should return 200 without body."""
        fake_backend.models_response = {"data": []}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http("127.0.0.1", server.port, "HEAD", "/health")
                assert resp["status"] == 200
            finally:
                server.shutdown()

    def test_unknown_path(self, fake_backend):
        """Unknown path should return 404."""
        fake_backend.models_response = {"data": []}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http("127.0.0.1", server.port, "GET", "/unknown")
                assert resp["status"] == 404
            finally:
                server.shutdown()

    # -- Used-models table (v0.8.4) --------------------------------------

    def test_post_records_model_usage(self, fake_backend):
        """Successful POST records the client model in the usage table."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 200
                from backend_adapter import model_usage as mu
                rows = mu.usage_snapshot()
                assert len(rows) == 1
                assert rows[0]["model"] == "test-model"
                assert rows[0]["calls"] == 1
                assert rows[0]["backend"] == "test"
                # проба мокнута в _setup_adapter → пустые эндпоинты, не «—»
                assert rows[0]["endpoints"] == {}
                assert rows[0]["probing"] is False
            finally:
                server.shutdown()

    # -- Usage counters (input/output tokens from backend usage blocks) ----

    def test_post_records_usage_tokens(self, fake_backend):
        """Non-stream POST with usage in the answer: input_tokens ==
        usage.prompt_tokens, output_tokens == usage.completion_tokens."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 34},
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 200
                from backend_adapter import model_usage as mu
                rows = mu.usage_snapshot()
                assert rows[0]["input_tokens"] == 12
                assert rows[0]["output_tokens"] == 34
            finally:
                server.shutdown()

    def test_post_without_usage_zero_tokens(self, fake_backend):
        """Backend answer without usage → input/output stay 0 (not counted)."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 200
                from backend_adapter import model_usage as mu
                rows = mu.usage_snapshot()
                assert rows[0]["calls"] == 1
                assert rows[0]["input_tokens"] == 0
                assert rows[0]["output_tokens"] == 0
            finally:
                server.shutdown()

    def test_repeat_post_increments_tokens(self, fake_backend):
        """Two POSTs of the same model double the token counters."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 34},
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                body = {"model": "test-model",
                        "messages": [{"role": "user", "content": "Hi"}],
                        "max_tokens": 100}
                for _ in range(2):
                    resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages", body=body)
                    assert resp["status"] == 200
                from backend_adapter import model_usage as mu
                rows = mu.usage_snapshot()
                assert rows[0]["calls"] == 2
                assert rows[0]["input_tokens"] == 24
                assert rows[0]["output_tokens"] == 68
            finally:
                server.shutdown()

    def test_backend_error_no_tokens(self, fake_backend):
        """Backend 400 (non-retry): client gets 400; error bodies carry no
        usage → input/output stay 0 (only the calls counter grows)."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        # 400 не входит в retry-список (429/502/503/504) — ровно одна попытка
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {}
        fake_backend.completions_status = 400
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 400
                from backend_adapter import model_usage as mu
                rows = mu.usage_snapshot()
                assert rows[0]["calls"] == 1
                assert rows[0]["input_tokens"] == 0
                assert rows[0]["output_tokens"] == 0
            finally:
                server.shutdown()

    def test_backend_502_retries_no_tokens(self, fake_backend):
        """Backend 502 is retried (default ADAPTER_RETRY=3): every attempt
        fails with an error body (no usage) → tokens stay 0, calls == 1."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {}
        fake_backend.completions_status = 502
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                # 3 попытки с backoff (2+4 с) — таймаут клиента больше
                # суммарной задержки, чтобы адаптер успел ответить 502.
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100},
                                  timeout=10)
                assert resp["status"] == 502  # все 3 попытки исчерпаны
                attempts = len(fake_backend.requests)
                assert attempts == 3  # было три HTTP-запроса к бэкенду
                from backend_adapter import model_usage as mu
                rows = mu.usage_snapshot()
                assert rows[0]["calls"] == 1
                assert rows[0]["input_tokens"] == 0
                assert rows[0]["output_tokens"] == 0
            finally:
                server.shutdown()

    def test_repeat_post_increments_no_reprobe(self, fake_backend):
        """Second POST of the same model increments, does not re-probe."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            # recording fake probe to count invocations
            from backend_adapter import model_usage as mu
            calls = []
            mu._probe_model_endpoints = (
                lambda backend, model: calls.append(model) or {"endpoints": {}, "errors": {}}
            )
            try:
                body = {"model": "test-model",
                        "messages": [{"role": "user", "content": "Hi"}],
                        "max_tokens": 100}
                for _ in range(2):
                    resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages", body=body)
                    assert resp["status"] == 200
                rows = mu.usage_snapshot()
                assert rows[0]["calls"] == 2
                assert calls == ["test-model"]  # проба только при первом обращении
            finally:
                server.shutdown()

    def test_strict_invalid_model_not_recorded(self, fake_backend):
        """Rejected model (400) must not be recorded in the usage table."""
        fake_backend.models_response = {"data": [{"id": "known-model"}]}
        fake_backend.completions_response = {}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "unknown-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 400
                from backend_adapter import model_usage as mu
                assert mu.usage_snapshot() == []
            finally:
                server.shutdown()

    def test_flag_off_no_probe_but_recorded(self, fake_backend):
        """ADAPTER_MODEL_USAGE_ENABLE=0 → row recorded, probe not fired."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = False
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            from backend_adapter import model_usage as mu
            calls = []
            mu._probe_model_endpoints = (
                lambda backend, model: calls.append(model) or {"endpoints": {}, "errors": {}}
            )
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 200
                rows = mu.usage_snapshot()
                assert len(rows) == 1
                assert rows[0]["calls"] == 1
                assert calls == []  # проба выключена флагом
            finally:
                server.shutdown()

    def test_tool_result_content_block_unconditional(self, fake_backend, capsys):
        """[TOOL_RESULT] content=… печатается БЕЗУСЛОВНО (флага
        ADAPTER_DEBUG_TOOLS больше нет — удалён реформой v0.8.6):
        tool_result в user-сообщении даёт content-блок в консоли через
        реальный logger._dr (не замокан), независимо от значения
        (отсутствующего) ADAPTER_DEBUG_TOOLS."""
        from backend_adapter import config, server as server_mod
        from backend_adapter.logger import _d as real_d, _dr as real_dr
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = True

        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend, mock_logger=False)
            # Реальные logger-хелперы печатают в stdout — восстанавливаем
            # оригиналы, которые _setup_adapter(mock_logger=False) не тронул.
            server_mod._d = real_d
            server_mod._dr = real_dr
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={
                        "model": "test-model",
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "result of the tool:"},
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_abc123",
                                    "content": "The temperature is 21 C.",
                                },
                            ],
                        }],
                        "max_tokens": 100,
                    },
                )
                assert resp["status"] == 200
                out = capsys.readouterr().out
                assert "[TOOL_RESULT] content=" in out
                assert "The temperature is 21 C." in out
            finally:
                server.shutdown()


# ===========================================================================
# Входные эндпоинты роутинга (v0.9.0): /v1/chat/completions и /v1/responses
# ===========================================================================

class TestInputEndpoints(ServerSetupMixin):
    """Новые входные POST-эндпоинты адаптера. TARGET-константы config
    выставляются прямым присваиванием (как runtime), кэш проб
    (_ENDPOINT_STATE) — через config.upsert_endpoint_state для бэкенда
    'test' (в _setup_adapter имя бэкенда = "test"). Дефолты conftest —
    zero-config: /v1/chat/completions и /v1/responses выключены (404),
    /v1/messages конвертируется."""

    def _enable(self, **targets: str) -> None:
        """Включить входы: установить TARGET-константы config."""
        from backend_adapter import config as cfg
        for var, value in targets.items():
            setattr(cfg, f"ADAPTER_{var.upper()}_TARGET", value)

    def _support(self, path: str, found: bool) -> None:
        """Наполнить кэш проб результатом для бэкенда 'test' (upsert)."""
        from backend_adapter import config as cfg
        state = cfg._ENDPOINT_STATE.setdefault(
            "test", {"at": 0.0, "endpoints": {}, "errors": {}}
        )
        state["endpoints"][path] = {"status": 200 if found else 404, "found": found}

    # -- нулевой конфиг: новые входы выключены ----------------------------

    def test_new_inputs_disabled_by_default(self, fake_backend):
        """Дефолт (TARGET=none): POST на новые входы → 404 «disabled»."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                for path, env in (("/v1/chat/completions", "ADAPTER_COMPLETIONS_TARGET"),
                                  ("/v1/responses", "ADAPTER_RESPONSES_TARGET")):
                    resp = _send_http(
                        "127.0.0.1", server.port, "POST", path,
                        body={"model": "test-model", "messages": []},
                    )
                    assert resp["status"] == 404
                    # Точное равенство текста ошибки (v0.9.4): substring
                    # пропускал дублирование префикса ADAPTER_.
                    assert json.loads(resp["body"])["error"] == (
                        f"endpoint is disabled ({env}=none)"
                    )
                # messages-дефолт жив: конвертация работает
                fake_backend.completions_response = {
                    "id": "chat1", "model": "test-model",
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                }
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                )
                assert resp["status"] == 200
            finally:
                server.shutdown()

    # -- /v1/chat/completions (passthrough E→E) ---------------------------

    def test_completions_passthrough_non_stream(self, fake_backend):
        """ADAPTER_COMPLETIONS_TARGET=completions + поддержка бэкендом
        /v1/chat/completions: тело уходит дословно (только model → resolved),
        ответ бэкенда отдаётся дословно, usage — prompt/completion."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        self._enable(completions="completions")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat123",
            "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/chat/completions", True)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/chat/completions",
                    body={"model": "test-model", "messages": [{"role": "user", "content": "Hi"}]},
                )
                assert resp["status"] == 200
                data = json.loads(resp["body"])
                # дословный ответ бэкенда
                assert data == fake_backend.completions_response
                # запрос ушёл на /v1/chat/completions с resolved model
                assert len(fake_backend.requests) == 1
                path, method, body = fake_backend.requests[0]
                assert path == "/v1/chat/completions"
                sent = json.loads(body)
                assert sent["model"] == "test-model"
                assert sent["messages"] == [{"role": "user", "content": "Hi"}]
                from backend_adapter import model_usage as mu
                rows = mu.usage_snapshot()
                assert rows[0]["input_tokens"] == 5
                assert rows[0]["output_tokens"] == 7
            finally:
                server.shutdown()

    # -- /v1/responses (passthrough E→E) ---------------------------------

    def test_responses_passthrough_non_stream(self, fake_backend):
        """ADAPTER_RESPONSES_TARGET=responses + поддержка бэкендом
        /v1/responses: дословный passthrough, usage — input/output."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        self._enable(responses="responses")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.responses_response = {
            "id": "resp_1",
            "model": "test-model",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello"}]}],
            "usage": {"input_tokens": 11, "output_tokens": 22},
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/responses", True)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/responses",
                    body={"model": "test-model", "input": [{"role": "user", "content": "Hi"}]},
                )
                assert resp["status"] == 200
                data = json.loads(resp["body"])
                assert data == fake_backend.responses_response
                assert len(fake_backend.requests) == 1
                path, _method, body = fake_backend.requests[0]
                assert path == "/v1/responses"
                sent = json.loads(body)
                assert sent["model"] == "test-model"
                assert sent["input"] == [{"role": "user", "content": "Hi"}]
                from backend_adapter import model_usage as mu
                rows = mu.usage_snapshot()
                assert rows[0]["input_tokens"] == 11
                assert rows[0]["output_tokens"] == 22
            finally:
                server.shutdown()

    # -- passthrough messages→messages (TARGET=messages) ------------------

    def test_messages_passthrough_when_target_messages(self, fake_backend):
        """ADAPTER_MESSAGES_TARGET=messages + поддержка /v1/messages: на
        антропик-совместимом бэкенде запрос уходит дословно (не
        конвертируется в completions), ответ — дословно."""
        self._enable(messages="messages")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        # /v1/messages на fake-бэкенде отвечает 200 без тела (extra_post_paths);
        # суть теста — маршрут: запрос ушёл на /v1/messages дословно (а не
        # конвертирован в /v1/chat/completions).
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/messages", True)
            # /v1/messages на fake-бэкенде — 200 без тела через extra_post_paths
            fake_backend.extra_post_paths = {"/v1/messages": 200}
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                )
                assert resp["status"] == 200
                # запрос ушёл НА /v1/messages дословно (без конвертации)
                assert len(fake_backend.requests) == 1
                path, _method, body = fake_backend.requests[0]
                assert path == "/v1/messages"
                sent = json.loads(body)
                assert sent["model"] == "test-model"
                assert "max_tokens" in sent  # антропик-поле не вычищено
            finally:
                server.shutdown()

    # -- passthrough messages→messages: system переносится в начало (v0.9.2) --

    def test_messages_passthrough_system_reordered(self, fake_backend):
        """ADAPTER_MESSAGES_TARGET=messages (passthrough E→E): system-сообщения,
        разбросанные по диалогу, переносятся в начало (без склейки), чтобы
        бэкенд с Jinja-шаблоном (raise_exception 'System message must be at
        the beginning') не упал 400. Тело иначе — как пришло (не конвертация)."""
        self._enable(messages="messages")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/messages", True)
            fake_backend.extra_post_paths = {"/v1/messages": 200}
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "max_tokens": 100,
                          "messages": [
                              {"role": "user", "content": "Хелло"},
                              {"role": "system", "content": "Правила"},
                              {"role": "user", "content": "Пока"},
                          ]},
                )
                assert resp["status"] == 200
                assert len(fake_backend.requests) == 1
                path, _method, body = fake_backend.requests[0]
                assert path == "/v1/messages"
                sent = json.loads(body)
                # system — первым; остальные роли сохраняют относительный порядок
                assert sent["messages"][0]["role"] == "system"
                assert sent["messages"][0]["content"] == "Правила"
                roles = [m["role"] for m in sent["messages"]]
                assert roles == ["system", "user", "user"]
                assert sent["messages"][1]["content"] == "Хелло"
                assert sent["messages"][2]["content"] == "Пока"
                # антропик-поля не вычищены (не конвертация), model/маппинг применён
                assert sent["model"] == "test-model"
                assert "max_tokens" in sent
            finally:
                server.shutdown()

    def test_messages_passthrough_system_first_unchanged(self, fake_backend):
        """Если system уже первым — тело уходит дословно (без перестановки)."""
        self._enable(messages="messages")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/messages", True)
            fake_backend.extra_post_paths = {"/v1/messages": 200}
            try:
                body = {"model": "test-model",
                        "messages": [
                            {"role": "system", "content": "rules"},
                            {"role": "user", "content": "Hi"},
                        ],
                        "max_tokens": 100}
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages", body=body)
                assert resp["status"] == 200
                sent = json.loads(fake_backend.requests[0][2])
                assert [m["role"] for m in sent["messages"]] == ["system", "user"]
            finally:
                server.shutdown()

    def test_messages_passthrough_stream_system_reordered(self, fake_backend):
        """messages→messages + stream=true: нормализация system в начало
        применяется и к стрим-запросу (тело запроса уходит до SSE-релея)."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        self._enable(messages="messages")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.sse_lines = [
            b'data: {"type": "message_start", "message": {"role": "assistant", "content": []}}\n\n',
            b'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}}\n\n',
            b'data: {"type": "message_stop"}\n\n',
        ]
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/messages", True)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "max_tokens": 100,
                          "stream": True,
                          "messages": [
                              {"role": "user", "content": "first"},
                              {"role": "system", "content": "rules"},
                          ]},
                )
                assert resp["status"] == 200
                # запрос на бэкенд ушёл с system первым
                assert len(fake_backend.requests) == 1
                assert fake_backend.requests[0][0] == "/v1/messages"
                sent = json.loads(fake_backend.requests[0][2])
                assert sent["messages"][0]["role"] == "system"
                assert sent["messages"][1]["role"] == "user"
            finally:
                server.shutdown()

    # -- авто: passthrough>convert / нет маршрута -------------------------

    def test_messages_auto_passthrough_priority(self, fake_backend):
        """messages auto: passthrough messages→messages приоритетнее
        конверсии, когда бэкенд поддерживает /v1/messages."""
        self._enable(messages="auto")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/messages", True)
            fake_backend.extra_post_paths = {"/v1/messages": 200}
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                )
                assert resp["status"] == 200
                assert len(fake_backend.requests) == 1
                assert fake_backend.requests[0][0] == "/v1/messages"
            finally:
                server.shutdown()

    def test_messages_auto_falls_back_to_convert(self, fake_backend):
        """messages auto без /v1/messages у бэкенда: convert в completions."""
        self._enable(messages="auto")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/messages", False)
            self._support("/v1/chat/completions", True)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                )
                assert resp["status"] == 200
                # ушло в /v1/chat/completions (конвертация)
                assert len(fake_backend.requests) == 1
                assert fake_backend.requests[0][0] == "/v1/chat/completions"
                data = json.loads(resp["body"])
                assert data["role"] == "assistant"  # антропик-ответ
            finally:
                server.shutdown()

    def test_messages_auto_no_route(self, fake_backend):
        """messages auto: ни /v1/messages, ни реализованной конверсии —
        нет found=True → 400 «no route»."""
        self._enable(messages="auto")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            # кэш проб пуст (None) — auto строг: passthrough не выбирается
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                )
                assert resp["status"] == 400
                assert "no route" in resp["body"]
                # запрос до бэкенда не дошёл
                assert fake_backend.requests == []
            finally:
                server.shutdown()

    # -- 502 при подтверждённом отказе бэкенда ----------------------------

    def test_completions_passthrough_rejected_502(self, fake_backend):
        """Явный TARGET=completions, но бэкенд подтверждённо (found=False)
        не поддерживает /v1/chat/completions → 502, запрос не уходит."""
        self._enable(completions="completions")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/chat/completions", False)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/chat/completions",
                    body={"model": "test-model", "messages": []},
                )
                assert resp["status"] == 502
                assert "does not support completions" in resp["body"]
                assert fake_backend.requests == []
            finally:
                server.shutdown()

    # -- strict-модель на новых входах ------------------------------------

    def test_strict_models_on_completions_input(self, fake_backend):
        """strict-проверка модели работает и на новом входе: неизвестная
        модель → 400 ДО роутинга/бэкенда."""
        self._enable(completions="completions")
        fake_backend.models_response = {"data": [{"id": "known-model"}]}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/chat/completions", True)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/chat/completions",
                    body={"model": "unknown-model", "messages": []},
                )
                assert resp["status"] == 400
                assert "not available" in resp["body"]
                assert fake_backend.requests == []
            finally:
                server.shutdown()

    # -- SSE-релей (passthrough-стрим) ------------------------------------

    def test_completions_passthrough_stream_relay(self, fake_backend):
        """ADAPTER_COMPLETIONS_TARGET=completions + stream=true: поток
        бэкенда передаётся клиенту ДОСЛОВНО (SSE-релей E→E, без
        конвертации в антропик-события), usage — из финального чанка."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        self._enable(completions="completions")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.sse_lines = [
            'data: {"choices": [{"delta": {"content": "Hello"}}]}\n\n',
            'data: {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 4, "completion_tokens": 6}}\n\n',
            "data: [DONE]\n\n",
        ]
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/chat/completions", True)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/chat/completions",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "stream": True},
                )
                assert resp["status"] == 200
                # поток ушёл дословно, [OI]-формат не тронут
                assert "event: message_start" not in resp["body"]
                assert "message_stop" not in resp["body"]
                for line in fake_backend.sse_lines:
                    assert line in resp["body"]
                # usage из финального чанка попал в учёт
                from backend_adapter import model_usage as mu
                rows = mu.usage_snapshot()
                assert rows[0]["input_tokens"] == 4
                assert rows[0]["output_tokens"] == 6
            finally:
                server.shutdown()

    def test_responses_passthrough_stream_relay_usage(self, fake_backend):
        """responses-стрим: usage читается из вложенного response.usage
        события response.completed (не верхнеуровневого usage)."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_MODEL_USAGE_ENABLE = True
        self._enable(responses="responses")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.sse_lines = [
            b'data: {"type": "response.output_text.delta", "delta": "Hi"}\n\n',
            b'data: {"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 9}}}\n\n',
        ]
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/responses", True)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/responses",
                    body={"model": "test-model",
                          "input": [{"role": "user", "content": "Hi"}],
                          "stream": True},
                )
                assert resp["status"] == 200
                # поток дословно (ни одного антропик-события)
                assert "message_start" not in resp["body"]
                assert "response.completed" in resp["body"]
                from backend_adapter import model_usage as mu
                rows = mu.usage_snapshot()
                assert rows[0]["input_tokens"] == 3
                assert rows[0]["output_tokens"] == 9
            finally:
                server.shutdown()


# ===========================================================================
# Учёт сессий (v0.9.2): реестр session_registry заполняется в do_POST
# ===========================================================================

class TestSessionAccounting(ServerSetupMixin):
    """Точки учёта таблицы WEBUI «Sessions»: register после routing.decide
    (включая reject/disabled) и в 400-ветках до него (пустой кортеж);
    record_error — из общих _send_json/_send_raw по финальному статусу >= 400
    (no-op для незарегистрированной сессии). Ключ строки — кортеж
    (session, agent, model, backend, route): смена модели/маршрута даёт
    новую строку, возврат к прежнему кортежу — ту же (calls++)."""

    def _enable(self, **targets: str) -> None:
        from backend_adapter import config as cfg
        for var, value in targets.items():
            setattr(cfg, f"ADAPTER_{var.upper()}_TARGET", value)

    def _support(self, path: str, found: bool) -> None:
        from backend_adapter import config as cfg
        state = cfg._ENDPOINT_STATE.setdefault(
            "test", {"at": 0.0, "endpoints": {}, "errors": {}}
        )
        state["endpoints"][path] = {"status": 200 if found else 404, "found": found}

    def _sessions(self):
        from backend_adapter import session_registry
        return session_registry.sessions_snapshot()

    def test_session_registered_with_route(self, fake_backend):
        """Запрос messages→messages: строка сессии с agent/model/backend/route."""
        self._enable(messages="messages")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.extra_post_paths = {"/v1/messages": 200}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/messages", True)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                    headers={"X-Claude-Code-Session-Id": "sess-abc",
                             "User-Agent": "claude-cli/2.1.236 (external, cli)"},
                )
                assert resp["status"] == 200
                rows = self._sessions()
                assert len(rows) == 1
                row = rows[0]
                assert row["session"] == "sess-abc"
                # агент — User-Agent до первого пробела
                assert row["agent"] == "claude-cli/2.1.236"
                assert row["model"] == "test-model"
                assert row["backend"] == "test"
                assert row["route"] == "passthrough messages→messages"
                assert row["calls"] == 1
                assert row["errors"] == 0
            finally:
                server.shutdown()

    # ── Session id: список заголовков-кандидатов (v0.9.4) ──

    def test_session_from_opencode_header(self, fake_backend):
        """QwenCode шлёт id не в [CC]-заголовке: id берётся из второго
        кандидата дефолтного списка, а не подменяется на «unknown»."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                    headers={"x-opencode-session": "261d9bea-eefd-44f8-aa6d-d2d3b210ad90",
                             "User-Agent": "QwenCode/0.23.2 (darwin; arm64)"},
                )
                assert resp["status"] == 200
                row = self._sessions()[0]
                assert row["session"] == "261d9bea-eefd-44f8-aa6d-d2d3b210ad90"
                assert row["agent"] == "QwenCode/0.23.2"
            finally:
                server.shutdown()

    def test_both_session_headers_first_wins(self, fake_backend):
        """Оба заголовка заданы — побеждает ПЕРВЫЙ в списке
        (X-Claude-Code-Session-Id), значение второго игнорируется."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                    headers={"X-Claude-Code-Session-Id": "sess-first",
                             "x-opencode-session": "sess-second"},
                )
                assert resp["status"] == 200
                assert self._sessions()[0]["session"] == "sess-first"
            finally:
                server.shutdown()

    def test_custom_session_header_list(self, fake_backend):
        """ADAPTER_SESSION_HEADER=x-opencode-session: [CC]-заголовок больше не
        читается, id берётся только из явно указанного."""
        from backend_adapter import config as cfg
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        old = cfg.ADAPTER_SESSION_HEADER
        cfg.ADAPTER_SESSION_HEADER = "x-opencode-session"
        try:
            with fake_backend:
                server = self._setup_adapter(fake_backend)
                try:
                    resp = _send_http(
                        "127.0.0.1", server.port, "POST", "/v1/messages",
                        body={"model": "test-model",
                              "messages": [{"role": "user", "content": "Hi"}],
                              "max_tokens": 100},
                        headers={"X-Claude-Code-Session-Id": "sess-ignored",
                                 "x-opencode-session": "sess-opencode"},
                    )
                    assert resp["status"] == 200
                    assert self._sessions()[0]["session"] == "sess-opencode"
                finally:
                    server.shutdown()
        finally:
            cfg.ADAPTER_SESSION_HEADER = old

    def test_no_session_header_is_unknown(self, fake_backend):
        """Ни одного заголовка-кандидата → session == «unknown» (штатный
        фолбэк, не падение)."""
        from backend_adapter import session_log as session_log_mod
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                    headers={"User-Agent": "curl/8.0"},
                )
                assert resp["status"] == 200
                assert self._sessions()[0]["session"] == session_log_mod.UNKNOWN_SESSION_ID
            finally:
                server.shutdown()

    # ── Session id: JSON-заголовки (Codex CLI, v0.9.4) ──

    _CODEX_METADATA = (
        '{"installation_id":"4b895ef6-f5d3-4694-bc42-9a4df0fe23a9",'
        '"session_id":"01a09245-ca32-7343-a9a7-3a4c879ede7b",'
        '"thread_id":"01a09245-ca32-7343-a9a7-3a4c879ede7b",'
        '"request_kind":"turn"}'
    )

    def test_session_from_codex_json_header(self, fake_backend):
        """Codex CLI шлёт id внутри JSON-заголовка x-codex-turn-metadata:
        кандидат «Имя:ключ» разбирает значение и берёт session_id."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                    headers={"x-codex-turn-metadata": self._CODEX_METADATA,
                             "User-Agent": "codex_cli_rs/0.20.0"},
                )
                assert resp["status"] == 200
                assert self._sessions()[0]["session"] == \
                    "01a09245-ca32-7343-a9a7-3a4c879ede7b"
            finally:
                server.shutdown()

    def test_codex_json_header_bad_value_is_unknown(self, fake_backend):
        """Битый JSON / отсутствие ключа / нестроковое значение → кандидат
        пропускается, сессия остаётся «unknown» (не падение)."""
        from backend_adapter import session_log as session_log_mod
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        bad_values = [
            "not-json-at-all",
            '{"thread_id":"01a09245-ca32-7343-a9a7-3a4c879ede7b"}',
            '{"session_id":12345}',
        ]
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                for value in bad_values:
                    resp = _send_http(
                        "127.0.0.1", server.port, "POST", "/v1/messages",
                        body={"model": "test-model",
                              "messages": [{"role": "user", "content": "Hi"}],
                              "max_tokens": 100},
                        headers={"x-codex-turn-metadata": value},
                    )
                    assert resp["status"] == 200
                assert self._sessions()[0]["session"] == \
                    session_log_mod.UNKNOWN_SESSION_ID
            finally:
                server.shutdown()

    def test_codex_json_header_custom_list(self, fake_backend):
        """ADAPTER_SESSION_HEADER=x-codex-turn-metadata:session_id: читается
        только JSON-кандидат, штатный X-Claude-Code-Session-Id игнорируется."""
        from backend_adapter import config as cfg
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        old = cfg.ADAPTER_SESSION_HEADER
        cfg.ADAPTER_SESSION_HEADER = "x-codex-turn-metadata:session_id"
        try:
            with fake_backend:
                server = self._setup_adapter(fake_backend)
                try:
                    resp = _send_http(
                        "127.0.0.1", server.port, "POST", "/v1/messages",
                        body={"model": "test-model",
                              "messages": [{"role": "user", "content": "Hi"}],
                              "max_tokens": 100},
                        headers={"X-Claude-Code-Session-Id": "sess-ignored",
                                 "x-codex-turn-metadata": self._CODEX_METADATA},
                    )
                    assert resp["status"] == 200
                    assert self._sessions()[0]["session"] == \
                        "01a09245-ca32-7343-a9a7-3a4c879ede7b"
                finally:
                    server.shutdown()
        finally:
            cfg.ADAPTER_SESSION_HEADER = old

    def test_client_request_id_not_in_default_list(self, fake_backend):
        """x-client-request-id — opt-in (generic per-request заголовок): без
        явного указания в списке он не подменяет сессию, фолбэк «unknown»."""
        from backend_adapter import session_log as session_log_mod
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                    headers={"x-client-request-id": "01a09245-ca32-7343-a9a7-3a4c879ede7b"},
                )
                assert resp["status"] == 200
                assert self._sessions()[0]["session"] == \
                    session_log_mod.UNKNOWN_SESSION_ID
            finally:
                server.shutdown()

    def test_codex_json_beats_client_request_id(self, fake_backend):
        """Оба codex-заголовка заданы с разными uuid: побеждает JSON-кандидат
        (он раньше в дефолтном списке), x-client-request-id игнорируется."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                    headers={"x-codex-turn-metadata": self._CODEX_METADATA,
                             "x-client-request-id": "other-request-uuid"},
                )
                assert resp["status"] == 200
                assert self._sessions()[0]["session"] == \
                    "01a09245-ca32-7343-a9a7-3a4c879ede7b"
            finally:
                server.shutdown()

    def test_convert_route_recorded(self, fake_backend):
        """messages→completions (дефолт): route = convert messages→completions."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "test-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                    headers={"X-Claude-Code-Session-Id": "sess-conv"},
                )
                assert resp["status"] == 200
                row = self._sessions()[0]
                assert row["route"] == "convert messages→completions"
            finally:
                server.shutdown()

    def test_error_increments_counter(self, fake_backend):
        """400 валидации (неизвестная модель, strict) → errors +1."""
        fake_backend.models_response = {"data": [{"id": "known-model"}]}
        fake_backend.completions_response = {}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/messages",
                    body={"model": "unknown-model",
                          "messages": [{"role": "user", "content": "Hi"}],
                          "max_tokens": 100},
                    headers={"X-Claude-Code-Session-Id": "sess-err"},
                )
                assert resp["status"] == 400
                row = self._sessions()[0]
                assert row["errors"] == 1
                # маршрут не доехал (400 до routing.decide) — model пуст
                assert row["model"] == ""
            finally:
                server.shutdown()

    def test_disabled_increments_errors_and_records_route(self, fake_backend):
        """TARGET=none (disabled) → 404 + строка с route=disabled и errors=1."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/chat/completions",
                    body={"model": "test-model", "messages": []},
                    headers={"X-Claude-Code-Session-Id": "sess-dis"},
                )
                assert resp["status"] == 404
                row = self._sessions()[0]
                assert row["route"] == "disabled"
                assert row["model"] == "test-model"
                assert row["backend"] == "test"
                assert row["errors"] == 1
            finally:
                server.shutdown()

    def test_repeated_calls_accumulate(self, fake_backend):
        """Повторные обращения одной сессии: calls растёт, строка одна."""
        self._enable(messages="messages")
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.extra_post_paths = {"/v1/messages": 200}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            self._support("/v1/messages", True)
            try:
                for _ in range(3):
                    resp = _send_http(
                        "127.0.0.1", server.port, "POST", "/v1/messages",
                        body={"model": "test-model",
                              "messages": [{"role": "user", "content": "Hi"}],
                              "max_tokens": 100},
                        headers={"X-Claude-Code-Session-Id": "sess-rep"},
                    )
                    assert resp["status"] == 200
                rows = self._sessions()
                assert len(rows) == 1
                assert rows[0]["calls"] == 3
                assert rows[0]["errors"] == 0
            finally:
                server.shutdown()

    def test_model_change_creates_new_row(self, fake_backend):
        """Смена модели агентом в одной сессии → НОВАЯ строка (кортеж иной)."""
        from backend_adapter import config as cfg
        self._enable(messages="messages")
        fake_backend.models_response = {
            "data": [{"id": "test-model"}, {"id": "test-model-2"}]
        }
        fake_backend.extra_post_paths = {"/v1/messages": 200}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            cfg._MODEL_TO_BACKEND["test-model-2"] = ("test", cfg._BACKENDS[0])
            cfg._AVAILABLE_MODELS["test-model-2"] = {"id": "test-model-2"}
            self._support("/v1/messages", True)
            try:
                for model in ("test-model", "test-model-2"):
                    resp = _send_http(
                        "127.0.0.1", server.port, "POST", "/v1/messages",
                        body={"model": model,
                              "messages": [{"role": "user", "content": "Hi"}],
                              "max_tokens": 100},
                        headers={"X-Claude-Code-Session-Id": "sess-mm",
                                 "User-Agent": "claude-cli/2.1.236"},
                    )
                    assert resp["status"] == 200
                rows = self._sessions()
                assert len(rows) == 2
                assert {r["model"] for r in rows} == {"test-model", "test-model-2"}
                assert all(r["calls"] == 1 for r in rows)
            finally:
                server.shutdown()

    def test_return_to_previous_model_reuses_row(self, fake_backend):
        """m1 → m2 → m1: возврат к прежнему кортежу — ТА ЖЕ строка, calls++."""
        from backend_adapter import config as cfg
        self._enable(messages="messages")
        fake_backend.models_response = {
            "data": [{"id": "test-model"}, {"id": "test-model-2"}]
        }
        fake_backend.extra_post_paths = {"/v1/messages": 200}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            cfg._MODEL_TO_BACKEND["test-model-2"] = ("test", cfg._BACKENDS[0])
            cfg._AVAILABLE_MODELS["test-model-2"] = {"id": "test-model-2"}
            self._support("/v1/messages", True)
            try:
                for model in ("test-model", "test-model-2", "test-model"):
                    resp = _send_http(
                        "127.0.0.1", server.port, "POST", "/v1/messages",
                        body={"model": model,
                              "messages": [{"role": "user", "content": "Hi"}],
                              "max_tokens": 100},
                        headers={"X-Claude-Code-Session-Id": "sess-rt",
                                 "User-Agent": "claude-cli/2.1.236"},
                    )
                    assert resp["status"] == 200
                rows = self._sessions()
                assert len(rows) == 2
                m1 = next(r for r in rows if r["model"] == "test-model")
                assert m1["calls"] == 2
                assert next(
                    r for r in rows if r["model"] == "test-model-2"
                )["calls"] == 1
            finally:
                server.shutdown()

    def test_unknown_path_does_not_create_session(self, fake_backend):
        """404 на не-входной путь строку НЕ создаёт (register после return)."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            try:
                resp = _send_http(
                    "127.0.0.1", server.port, "POST", "/v1/embeddings",
                    body={"model": "test-model"},
                    headers={"X-Claude-Code-Session-Id": "sess-404"},
                )
                assert resp["status"] == 404
                assert self._sessions() == []
            finally:
                server.shutdown()

    def test_invalid_json_creates_session_without_route(self, fake_backend):
        """400 «Invalid JSON»: register с пустым кортежем, маршрута нет."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        with fake_backend:
            server = self._setup_adapter(fake_backend)
            sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            try:
                raw = b"{not json"
                sock.sendall(
                    b"POST /v1/messages HTTP/1.0\r\nHost: localhost\r\n"
                    b"X-Claude-Code-Session-Id: sess-badjson\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(raw)).encode() + b"\r\n\r\n" + raw
                )
                response = b""
                while True:
                    try:
                        sock.settimeout(3)
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        response += chunk
                    except socket.timeout:
                        break
                assert b" 400 " in response.split(b"\r\n", 1)[0]
            finally:
                sock.close()
                server.shutdown()
            row = self._sessions()[0]
            assert row["session"] == "sess-badjson"
            assert row["route"] == ""  # до routing.decide не дошли
            assert row["errors"] == 1

class TestErrFileProtocol(ServerSetupMixin):
    """Протокол .err-инцидентов (v0.9.0): финальный 4xx/5xx реального
    прокси-запроса пишет session-<ts>-<safe8>.err в LOGPATH (безусловный
    канал — вне ADAPTER_DEBUG_ENABLE/PARTS/TRIM). Тесты ставят LOGPATH на
    tmp_path и НЕ мокают write_error_file (mock_err=False)."""

    def _setup(self, fake_backend, tmp_path, mock_logger=True):
        """Настроить сессионные пути на tmp_path и адаптер с живым .err-каналом."""
        from backend_adapter import session_log as session_log_mod
        session_log_mod._DEBUG_IS_DIR = True
        session_log_mod._TRACE_IS_DIR = True
        session_log_mod._DEBUG_PATH = str(tmp_path)
        session_log_mod._TRACE_PATH = str(tmp_path)
        session_log_mod._session_logs.clear()
        session_log_mod._session_file_ts.clear()
        return self._setup_adapter(fake_backend, mock_logger=mock_logger, mock_err=False)

    def _err_files(self, tmp_path):
        return sorted(tmp_path.glob("session-*.err"))

    # -- 400 от бэкенда (не-retry) ---------------------------------------

    def test_backend_400_writes_err_file(self, fake_backend, tmp_path):
        """400-ответ бэкенда → .err создан; не-стрим ветка; тело и запрос полные."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_DEBUG = False  # файловая запись debug выключена — .err всё равно пишется
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {"error": {"message": "bad request from backend"}}
        fake_backend.completions_status = 400
        with fake_backend:
            server = self._setup(fake_backend, tmp_path)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 400
            finally:
                server.shutdown()
        errs = self._err_files(tmp_path)
        assert len(errs) == 1
        content = errs[0].read_text(encoding="utf-8")
        assert "final_status=400" in content
        assert 'model=test-model' in content
        assert "/v1/chat/completions" in content
        # Полный запрос [OI] (out_body) и полная ошибка бэкенда
        assert '"max_tokens": 100' in content
        assert "bad request from backend" in content
        assert "==================== ERROR ====================" in content

    def test_backend_400_stream_writes_err_file(self, fake_backend, tmp_path):
        """400 в стрим-ветке (stream=true, ошибка до начала потока) → .err."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {"error": {"message": "stream denied"}}
        fake_backend.completions_status = 400
        with fake_backend:
            server = self._setup(fake_backend, tmp_path)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100,
                                        "stream": True})
                assert resp["status"] == 400
            finally:
                server.shutdown()
        errs = self._err_files(tmp_path)
        assert len(errs) == 1
        content = errs[0].read_text(encoding="utf-8")
        assert "final_status=400" in content
        assert "stream denied" in content

    # -- 502 после ретраев -------------------------------------------------

    def test_backend_502_retries_writes_err_file(self, fake_backend, tmp_path):
        """502 (3 попытки с backoff) → .err один (финальный инцидент)."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {"error": {"message": "upstream boom"}}
        fake_backend.completions_status = 502
        with fake_backend:
            server = self._setup(fake_backend, tmp_path)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100},
                                  timeout=15)
                assert resp["status"] == 502
                assert len(fake_backend.requests) == 3
            finally:
                server.shutdown()
        errs = self._err_files(tmp_path)
        assert len(errs) == 1  # один .err на запрос, не на попытку
        assert "final_status=502" in errs[0].read_text(encoding="utf-8")

    # -- успех / локальные 400 адаптера — .err НЕ пишется ------------------

    def test_success_no_err_file(self, fake_backend, tmp_path):
        """200-успех с инвариантом в порядке (первое сообщение system) →
        .err не создан."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup(fake_backend, tmp_path)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "system", "content": "Sys"},
                                                     {"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 200
            finally:
                server.shutdown()
        assert self._err_files(tmp_path) == []

    def test_success_non_system_first_writes_warn(self, fake_backend, tmp_path):
        """200-успех, но первое сообщение user → WARNING-блок в .err (v0.9.1):
        инвариант конвертации нарушен — пишется безусловно, на успешном ответе."""
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "id": "chat1", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        with fake_backend:
            server = self._setup(fake_backend, tmp_path)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 200
            finally:
                server.shutdown()
        errs = self._err_files(tmp_path)
        assert len(errs) == 1
        content = errs[0].read_text(encoding="utf-8")
        # ERROR-блока нет — только WARNING (запрос прошёл успешно)
        assert "==================== ERROR ====================" not in content
        assert "==================== WARNING ====================" in content
        assert "==================== END WARNING ====================" in content
        assert "final_status" not in content
        assert "model=test-model" in content
        # Полный [OI]-запрос и текст WARN
        assert '"max_tokens": 100' in content
        assert '"role": "user"' in content
        assert "[WARN]" in content
        assert "First message is NOT system" in content

    def test_local_400_no_backend_no_err_file(self, fake_backend, tmp_path):
        """400 ДО бэкенда (нет /v1/messages, strict-модель) → .err не создан:
        бэкенд не участвовал, инцидента взаимодействия нет."""
        fake_backend.models_response = {"data": [{"id": "known-model"}]}
        fake_backend.completions_response = {}
        with fake_backend:
            server = self._setup(fake_backend, tmp_path)
            try:
                resp = _send_http("127.0.0.1", server.port, "GET", "/unknown")
                assert resp["status"] == 404
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "unknown-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 400
            finally:
                server.shutdown()
        assert self._err_files(tmp_path) == []

    def test_err_file_redacts_by_default(self, fake_backend, tmp_path):
        """Санитайзер .err по умолчанию (SENSITIVE=0): секреты маскируются."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_SENSITIVE_LOGGING_ENABLE = False
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "error": {"message": "invalid Authorization: Bearer sk-live-abcdef123456"}
        }
        fake_backend.completions_status = 401
        with fake_backend:
            server = self._setup(fake_backend, tmp_path)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 401
            finally:
                server.shutdown()
        content = self._err_files(tmp_path)[0].read_text(encoding="utf-8")
        assert "sk-live-abcdef123456" not in content
        assert "REDACTED" in content

    def test_err_file_full_at_sensitive_one(self, fake_backend, tmp_path):
        """При SENSITIVE=1 .err несёт полные данные (без redact)."""
        from backend_adapter import config as cfg
        cfg.ADAPTER_SENSITIVE_LOGGING_ENABLE = True
        fake_backend.models_response = {"data": [{"id": "test-model"}]}
        fake_backend.completions_response = {
            "error": {"message": "invalid Authorization: Bearer sk-live-abcdef123456"}
        }
        fake_backend.completions_status = 401
        with fake_backend:
            server = self._setup(fake_backend, tmp_path)
            try:
                resp = _send_http("127.0.0.1", server.port, "POST", "/v1/messages",
                                  body={"model": "test-model",
                                        "messages": [{"role": "user", "content": "Hi"}],
                                        "max_tokens": 100})
                assert resp["status"] == 401
            finally:
                server.shutdown()
        content = self._err_files(tmp_path)[0].read_text(encoding="utf-8")
        assert "sk-live-abcdef123456" in content
        assert "REDACTED" not in content
