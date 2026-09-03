"""Integration tests for backend_adapter.server — HTTP server with fake backend."""
import json
import os
import socket
import sys
import threading
import time


def _send_http(host, port, method, path, body=None, headers=None):
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
                sock.settimeout(3)
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
