#!/usr/bin/env python3
"""Unit + HTTP tests for backend_adapter.webui_status — status page at "/".

Tests cover: _config_snapshot() mode detection (legacy / multi-backend /
standalone), _collect_endpoints() grouping, _probe_live() bounded live probe
(success / empty list / connection error / own timeout, NOT ADAPTER_TIMEOUT),
GET "/" HTML (version from context, endpoint names, models, standalone
notice), and POST "/" live refresh (mocked probe, probe failure must not
crash the response).
"""
import json
import os
import sys
import threading
from unittest import mock

import pytest

# Autouse fresh_env deletes all backend_adapter* modules and re-imports
# config with the default test env (ADAPTER_BACKEND_BASE=http://localhost:9998,
# ADAPTER_BACKEND_CONFIG=""). So config._BACKEND_LEGACY is True, BACKEND_BASE
# is the env value, and _AVAILABLE_MODELS/_BACKENDS are empty. Tests assign
# config globals directly on the *current* module instance (same one that
# webui_status imported — fresh per test, so no cross-test pollution).


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _start_server(root_dir: str):
    from backend_adapter import webserver
    httpd = webserver.serve(root_dir, "0.0.0-test", port=0)
    assert httpd is not None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_port


def _http_get(port: int, path: str):
    import socket
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
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
        status = int(text.split(" ", 2)[1])
        parts = text.split("\r\n\r\n", 1)
        body = parts[1] if len(parts) > 1 else ""
        return status, body
    finally:
        sock.close()


def _http_post(port: int, path: str):
    import socket
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(f"POST {path} HTTP/1.0\r\nHost: localhost\r\n"
                     f"Content-Length: 0\r\n\r\n".encode())
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
        status = int(text.split(" ", 2)[1])
        parts = text.split("\r\n\r\n", 1)
        body = parts[1] if len(parts) > 1 else ""
        return status, body
    finally:
        sock.close()


def _fresh_modules():
    """Переимпорт свежих модулей (после autouse fresh_env) в экземплярах."""
    from backend_adapter import config
    from backend_adapter import webui_status
    return config, webui_status


# ---------------------------------------------------------------------------
# TestConfigSnapshot — режимы: legacy / multi / standalone
# ---------------------------------------------------------------------------

class TestConfigSnapshot:
    def test_legacy_with_models(self):
        # ADAPTER_BACKEND_BASE задан fresh_env (localhost:9998) и legacy
        # включён (ADAPTER_BACKEND_CONFIG пуст). Модели — из _AVAILABLE_MODELS.
        config, ws = _fresh_modules()
        config._AVAILABLE_MODELS["m-one"] = {"id": "m-one"}
        config._AVAILABLE_MODELS["m-two"] = {"id": "m-two"}
        snap = ws._config_snapshot()
        assert snap["mode"] == "legacy"
        assert len(snap["endpoints"]) == 1
        ep = snap["endpoints"][0]
        assert ep["name"] == "legacy"
        assert ep["base"] == "http://localhost:9998"
        assert ep["models"] == ["m-one", "m-two"]
        assert ep["status"] == "ok"
        assert snap["note"] is None

    def test_legacy_env_unset_no_models(self, monkeypatch):
        # ВАЖНО: BACKEND_BASE имеет дефолт config.py даже без env — страница
        # не должна показывать фантомный бэкенд, если env реально не задан.
        config, ws = _fresh_modules()
        monkeypatch.delenv("ADAPTER_BACKEND_BASE", raising=False)
        snap = ws._config_snapshot()
        assert snap["endpoints"] == []
        assert snap["mode"] == "standalone"
        assert snap["note"] is not None

    def test_multi_backend_grouping(self):
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa", "key": "k-aaa"},
            {"name": "BBB", "base": "http://bbb", "key": "k-bbb"},
        ]
        config._MODEL_TO_BACKEND = {
            "m1": ("AAA", config._BACKENDS[0]),
            "m2": ("AAA", config._BACKENDS[0]),
            "m3": ("BBB", config._BACKENDS[1]),
        }
        snap = ws._config_snapshot()
        assert snap["mode"] == "multi-backend"
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        assert set(by_name) == {"AAA", "BBB"}
        assert by_name["AAA"]["models"] == ["m1", "m2"]
        assert by_name["AAA"]["status"] == "ok"
        assert by_name["BBB"]["models"] == ["m3"]
        assert by_name["BBB"]["status"] == "ok"
        assert snap["note"] is None

    def test_multi_unprobed_backend_not_ok(self):
        # Бэкенд в конфиге есть, но ни одной модели на нём не опрошено —
        # статус «не опрошен» (а не фантомный ok).
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa", "key": "k-aaa"},
            {"name": "BBB", "base": "http://bbb", "key": "k-bbb"},
        ]
        config._MODEL_TO_BACKEND = {"m1": ("AAA", config._BACKENDS[0])}
        snap = ws._config_snapshot()
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        assert by_name["AAA"]["status"] == "ok"
        assert by_name["BBB"]["status"] == "не опрошен"
        assert by_name["BBB"]["models"] == []

    def test_standalone_without_env(self, monkeypatch):
        # Режим standalone: ни бэкендов, ни env бэкенда, ни моделей.
        config, ws = _fresh_modules()
        monkeypatch.delenv("ADAPTER_BACKEND_BASE", raising=False)
        snap = ws._config_snapshot()
        assert snap["mode"] == "standalone"
        assert snap["endpoints"] == []
        assert snap["note"] and "ADAPTER_WEBUI_ENABLE" in snap["note"]

    def test_multi_endpoints_carry_keys(self):
        # key нужен POST-пробе; в HTML не выводится, но в данных должен быть.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa", "key": "secret-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m1": ("AAA", config._BACKENDS[0])}
        endpoints = ws._collect_endpoints()
        assert endpoints[0]["key"] == "secret-aaa"


# ---------------------------------------------------------------------------
# TestProbeLive
# ---------------------------------------------------------------------------

class TestProbeLive:
    def test_success_object_format(self):
        _, ws = _fresh_modules()
        payload = {"object": "list", "data": [{"id": "m1"}, {"id": "m2"}]}
        with mock.patch("urllib.request.urlopen") as m_urlopen:
            resp = mock.Mock()
            resp.read.return_value = json.dumps(payload).encode()
            m_urlopen.return_value = resp
            ok, models = ws._probe_live("http://example.com", "key")
        assert ok is True
        assert [m["id"] for m in models] == ["m1", "m2"]
        # URL строится как base + /v1/models
        req = m_urlopen.call_args[0][0]
        assert req.full_url == "http://example.com/v1/models"
        assert req.get_header("Authorization") == "Bearer key"

    def test_success_list_format(self):
        _, ws = _fresh_modules()
        with mock.patch("urllib.request.urlopen") as m_urlopen:
            resp = mock.Mock()
            resp.read.return_value = json.dumps([{"id": "m1"}]).encode()
            m_urlopen.return_value = resp
            ok, models = ws._probe_live("http://example.com/", "key")
        assert ok is True
        assert models == [{"id": "m1"}]

    def test_empty_list_ok(self):
        _, ws = _fresh_modules()
        with mock.patch("urllib.request.urlopen") as m_urlopen:
            resp = mock.Mock()
            resp.read.return_value = json.dumps({"data": []}).encode()
            m_urlopen.return_value = resp
            ok, models = ws._probe_live("http://example.com", "key")
        assert ok is True
        assert models == []

    def test_connection_error_returns_false(self):
        _, ws = _fresh_modules()
        with mock.patch("urllib.request.urlopen") as m_urlopen:
            m_urlopen.side_effect = Exception("Connection refused")
            ok, err = ws._probe_live("http://example.com", "key")
        assert ok is False
        assert "Connection refused" in str(err)

    def test_non_json_body_returns_false(self):
        _, ws = _fresh_modules()
        with mock.patch("urllib.request.urlopen") as m_urlopen:
            resp = mock.Mock()
            resp.read.return_value = b"<html>not json</html>"
            m_urlopen.return_value = resp
            ok, err = ws._probe_live("http://example.com", "key")
        assert ok is False
        assert "не JSON" in str(err)

    def test_timeout_is_own_short_not_adapter_timeout(self):
        # Жёсткий собственный таймаут (PROBE_TIMEOUT=5 с), а НЕ
        # ADAPTER_TIMEOUT (по умолчанию 300 с) — упавший бэкенд не должен
        # вешать страницу на 5 минут.
        config, ws = _fresh_modules()
        assert config.ADAPTER_TIMEOUT == 300
        assert ws.PROBE_TIMEOUT == 5.0
        with mock.patch("urllib.request.urlopen") as m_urlopen:
            resp = mock.Mock()
            resp.read.return_value = json.dumps({"data": []}).encode()
            m_urlopen.return_value = resp
            ws._probe_live("http://example.com", "key")
        _, kwargs = m_urlopen.call_args
        assert kwargs["timeout"] == 5.0


# ---------------------------------------------------------------------------
# HTTP: GET "/" и POST "/"
# ---------------------------------------------------------------------------

class TestStatusHTTP:
    def test_get_root_renders_version_and_legacy(self, tmp_path):
        config, _ = _fresh_modules()
        config._AVAILABLE_MODELS["legacy-model"] = {"id": "legacy-model"}
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/")
            assert status == 200
            assert "0.0.0-test" in body          # версия из контекста сервера
            assert "legacy" in body
            assert "legacy-model" in body        # модель из _AVAILABLE_MODELS
            assert "http://localhost:9998" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_get_root_multi_lists_backends_and_models(self, tmp_path):
        config, _ = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
            {"name": "BBB", "base": "http://bbb.local", "key": "k-bbb"},
        ]
        config._MODEL_TO_BACKEND = {
            "m-alpha": ("AAA", config._BACKENDS[0]),
            "m-beta": ("BBB", config._BACKENDS[1]),
        }
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/")
            assert status == 200
            assert "AAA" in body and "BBB" in body
            assert "m-alpha" in body and "m-beta" in body
            assert "multi-backend" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_get_root_standalone_notice(self, tmp_path, monkeypatch):
        config, _ = _fresh_modules()
        # Без env-бэкенда страница должна показать подсказку, а не
        # фантомный «example.com».
        monkeypatch.delenv("ADAPTER_BACKEND_BASE", raising=False)
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/")
            assert status == 200
            assert "standalone" in body
            assert "Данные адаптера недоступны" in body
            assert "example.com" not in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_live_refresh_success(self, tmp_path):
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}

        def fake_probe(base, key):
            return True, [{"id": "live-model-1"}, {"id": "live-model-2"}]

        with mock.patch.object(ws, "_probe_live", side_effect=fake_probe):
            httpd, port = _start_server(str(tmp_path))
            try:
                status, body = _http_post(port, "/")
                assert status == 200
                assert "live-model-1" in body
                assert "live-model-2" in body
                assert "Живая проверка выполнена" in body
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_post_probe_failure_does_not_crash(self, tmp_path):
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}

        def fake_probe(base, key):
            return False, "Connection refused by unit test"

        with mock.patch.object(ws, "_probe_live", side_effect=fake_probe):
            httpd, port = _start_server(str(tmp_path))
            try:
                status, body = _http_post(port, "/")
                assert status == 200
                assert "недоступен" in body
                assert "Connection refused" in body
            finally:
                httpd.shutdown()
                httpd.server_close()
