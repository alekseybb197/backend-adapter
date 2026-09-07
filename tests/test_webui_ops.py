#!/usr/bin/env python3
"""Unit + HTTP tests for backend_adapter.webui_ops — health/live/ready
endpoints on the shared WEBUI listener (v0.8.5, задача 3).

Contract (k8s-style probes, names fixed by the user): GET /healthz and its
alias /health and /live answer 200 application/json with status/version/
uptime/pid; GET /ready answers 200 {"status": "ready", ...} only when
config._BACKENDS is non-empty AND config._AVAILABLE_MODELS is non-empty
(backends configured AND warmed up — the start/last models probe succeeded);
otherwise 503 with a JSON body {"status": "not_ready", "reason": ...}
distinguishing the cause. Nested paths → 404; a non-GET method → 405
(endpoint base class default). Prefix matching does not collide: "/health"
does not match "/healthz".
"""
import json
import os
import socket
import threading
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _start_server(root_dir: str, version: str = "0.0.0-test"):
    """WEBUI-сервер на эфемерном порту (serve импортирует эндпойнт-модули,
    в т.ч. webui_ops, — реестр наполняется при импорте)."""
    from backend_adapter import webserver
    httpd = webserver.serve(root_dir, version, port=0)
    assert httpd is not None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_port


def _http_raw(port: int, method: str, path: str):
    """Сырой HTTP-ответ: (status, headers: dict, body_text)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        body = ""
        length = len(body.encode())
        extra = f"Content-Length: {length}\r\n" if method == "POST" else ""
        sock.sendall(
            f"{method} {path} HTTP/1.0\r\nHost: localhost\r\n{extra}\r\n".encode()
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
        text = response.decode("utf-8", "replace")
        head, _, body_text = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return status, headers, body_text
    finally:
        sock.close()


def _http_get(port: int, path: str):
    return _http_raw(port, "GET", path)


def _http_post(port: int, path: str):
    return _http_raw(port, "POST", path)


def _fresh_modules():
    """Свежие модули после autouse fresh_env."""
    from backend_adapter import config, webui_ops, webserver
    return config, webui_ops, webserver


def _backend(name: str = "AAA") -> dict:
    return {"name": name, "base": f"http://{name.lower()}.local", "key": "k"}


# ---------------------------------------------------------------------------
# Unit: _health_body / _ready_body
# ---------------------------------------------------------------------------

class TestHealthBody:
    def _ctx(self):
        return mock.Mock(version="0.0.0-test")

    def test_health_body_fields(self):
        """health-тело: status/version/uptime/pid — JSON, utf-8."""
        from backend_adapter import webui_ops
        data = json.loads(webui_ops._health_body(self._ctx()))
        assert data["status"] == "ok"
        assert data["version"] == "0.0.0-test"
        assert isinstance(data["uptime"], (int, float))
        assert data["uptime"] >= 0
        assert data["pid"] == os.getpid()

    def test_ready_body_ready_when_backends_and_models(self):
        """_BACKENDS и _AVAILABLE_MODELS непусты → (200, ready)."""
        config, webui_ops, _ = _fresh_modules()
        config._BACKENDS = [_backend()]
        config._AVAILABLE_MODELS["m"] = {"id": "m"}
        status, body = webui_ops._ready_body(self._ctx())
        data = json.loads(body)
        assert status == 200
        assert data["status"] == "ready"

    def test_ready_body_503_no_backends(self):
        """Пусто _BACKENDS → 503 not_ready, reason — про бэкенды."""
        config, webui_ops, _ = _fresh_modules()
        config._AVAILABLE_MODELS["m"] = {"id": "m"}
        status, body = webui_ops._ready_body(self._ctx())
        data = json.loads(body)
        assert status == 503
        assert data["status"] == "not_ready"
        assert "backend" in data["reason"]
        assert data["version"] == "0.0.0-test"

    def test_ready_body_503_models_empty(self):
        """Бэкенды есть, кэш моделей пуст (опрос не прошёл) → 503 + причина."""
        config, webui_ops, _ = _fresh_modules()
        config._BACKENDS = [_backend()]
        status, body = webui_ops._ready_body(self._ctx())
        data = json.loads(body)
        assert status == 503
        assert data["status"] == "not_ready"
        assert "models" in data["reason"]


# ---------------------------------------------------------------------------
# HTTP: эндпоинты на общем слушателе
# ---------------------------------------------------------------------------

class TestHealthHTTP:
    def _ready_server(self, tmp_path):
        """Сервер с прогретым состоянием (бэкенды + модели)."""
        config, webui_ops, _ = _fresh_modules()
        config._BACKENDS = [_backend()]
        config._MODEL_TO_BACKEND["m"] = (config._BACKENDS[0]["name"], config._BACKENDS[0])
        config._AVAILABLE_MODELS["m"] = {"id": "m"}
        return _start_server(str(tmp_path))

    def test_healthz_ok_json(self, tmp_path):
        httpd, port = self._ready_server(tmp_path)
        try:
            status, headers, body = _http_get(port, "/healthz")
            assert status == 200
            assert "application/json" in headers.get("content-type", "")
            data = json.loads(body)
            assert data["status"] == "ok"
            assert data["version"] == "0.0.0-test"
            assert data["pid"] == os.getpid()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_health_alias_ok_json(self, tmp_path):
        """/health — алиас /healthz: тот же JSON."""
        httpd, port = self._ready_server(tmp_path)
        try:
            status, _h, body = _http_get(port, "/health")
            assert status == 200
            assert json.loads(body)["status"] == "ok"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_live_ok_json(self, tmp_path):
        httpd, port = self._ready_server(tmp_path)
        try:
            status, _h, body = _http_get(port, "/live")
            assert status == 200
            assert json.loads(body)["status"] == "ok"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_ready_200_when_warmed(self, tmp_path):
        """Бэкенды настроены и прогреты (непустой кэш моделей) → 200 ready."""
        httpd, port = self._ready_server(tmp_path)
        try:
            status, _h, body = _http_get(port, "/ready")
            assert status == 200
            data = json.loads(body)
            assert data["status"] == "ready"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_ready_503_without_backends(self, tmp_path):
        """Пустой конфиг → 503 not_ready c JSON-телом (не send_error)."""
        config, webui_ops, _ = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, headers, body = _http_get(port, "/ready")
            assert status == 503
            assert "application/json" in headers.get("content-type", "")
            data = json.loads(body)
            assert data["status"] == "not_ready"
            assert data["reason"]
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_healthz_nested_path_404(self, tmp_path):
        """Непустой remainder (/healthz/x) → 404."""
        httpd, port = self._ready_server(tmp_path)
        try:
            for path in ("/healthz/x", "/live/x", "/health/sub"):
                status, _h, _b = _http_get(port, path)
                assert status == 404, path
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_on_live_is_405(self, tmp_path):
        """Не-GET метод на health/live → 405 (дефолт базового Endpoint)."""
        httpd, port = self._ready_server(tmp_path)
        try:
            for path in ("/healthz", "/live", "/ready", "/health"):
                status, _h, _b = _http_post(port, path)
                assert status == 405, path
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_health_and_healthz_do_not_collide(self, tmp_path):
        """Матчинг по самому длинному префиксу: /health НЕ перехватывает
        /healthz и наоборот; каждый отвечает своим JSON."""
        httpd, port = self._ready_server(tmp_path)
        try:
            _s1, _h1, body_health = _http_get(port, "/health")
            _s2, _h2, body_healthz = _http_get(port, "/healthz")
            assert json.loads(body_health)["status"] == "ok"
            assert json.loads(body_healthz)["status"] == "ok"
            # префиксы существуют как отдельные эндпойнты в реестре
            from backend_adapter import webserver
            prefixes = {ep.prefix for ep in webserver.Handler.endpoints}
            assert "/health" in prefixes and "/healthz" in prefixes
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_serve_registers_ops_endpoints(self, tmp_path):
        """serve() импортирует webui_ops — health/live/ready в реестре
        (классы зарегистрированы @register на импорте модуля)."""
        config, _webui_ops, webserver = _fresh_modules()
        root = str(tmp_path / "logs")
        os.makedirs(root)
        httpd = webserver.serve(root, "0.0.0-test", port=0)
        assert httpd is not None
        try:
            prefixes = {ep.prefix for ep in webserver.Handler.endpoints}
            assert {"/healthz", "/health", "/live", "/ready"} <= prefixes
        finally:
            httpd.server_close()
