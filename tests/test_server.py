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


class TestServer:
    """Integration tests for the HTTP server."""

    def _setup_adapter(self, fake_backend):
        """Set up adapter pointing at fake backend (single-backend YAML config).

        Uses direct attribute patching on already-loaded modules to avoid
        circular import issues from module deletion + reimport. The backend
        config is a one-entry multi-backend structure — единственный режим
        конфигурации бэкендов.
        """
        from backend_adapter import config, server as server_mod

        cfg = {"name": "test", "base": fake_backend.base_url, "key": "test-key"}
        config._BACKENDS = [cfg]
        config._BACKEND_BY_NAME = {"test": cfg}
        config._MODEL_TO_BACKEND = {"test-model": ("test", cfg)}
        config._DEFAULT_BACKEND = cfg
        config._AVAILABLE_MODELS["test-model"] = {"id": "test-model"}

        # Patch server's logger helpers to avoid file I/O blocking
        server_mod._d = lambda *a, **kw: None
        server_mod._dr = lambda *a, **kw: None
        server_mod._trace = lambda *a, **kw: None
        server_mod.write_debug_json = lambda *a, **kw: None

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
