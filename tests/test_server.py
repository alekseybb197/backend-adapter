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
        # .err-канал (v0.9.0) безусловен (не зависит от ADAPTER_DEBUG_ENABLE) —
        # в unit-контексте без LOGPATH его надо мокать, иначе ошибки пишутся
        # в дефолтный ./tmp/logs. Тесты самого .err ставят LOGPATH на tmp_path
        # и вызывают _setup_adapter(mock_err=False), чтобы канал был живым.
        if mock_err:
            server_mod.write_error_file = lambda *a, **kw: None

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
        """200-успех → .err не создан."""
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
        assert self._err_files(tmp_path) == []

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
