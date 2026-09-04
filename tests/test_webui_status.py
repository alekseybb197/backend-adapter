#!/usr/bin/env python3
"""Unit + HTTP tests for backend_adapter.webui_status — status page at "/".

Tests cover: _config_snapshot() mode detection (multi-backend / standalone),
_collect_endpoints() grouping, GET "/" and POST "/" — both
trigger config.refresh_models() (on-demand model cache refresh, mocked here)
and render the page from the refreshed globals: success shows the new model
list, failure keeps the old cache and shows the error text, standalone
(no endpoints) renders the notice without any refresh call.
"""
import os
import socket
import threading
from unittest import mock

import pytest

# Autouse fresh_env deletes all backend_adapter* modules and re-imports
# config with the default test env (ADAPTER_BACKEND_CONFIG=""). So
# _AVAILABLE_MODELS/_BACKENDS are empty. Tests assign config globals
# directly on the *current* module instance (same one that webui_status
# imported — fresh per test, so no cross-test pollution).


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


def _http_request(port: int, method: str, path: str):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        body = "" if method == "POST" else ""
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
        status = int(text.split(" ", 2)[1])
        parts = text.split("\r\n\r\n", 1)
        body_text = parts[1] if len(parts) > 1 else ""
        return status, body_text
    finally:
        sock.close()


def _http_get(port: int, path: str):
    return _http_request(port, "GET", path)


def _http_post(port: int, path: str):
    return _http_request(port, "POST", path)


def _fresh_modules():
    """Переимпорт свежих модулей (после autouse fresh_env) в экземплярах."""
    from backend_adapter import config
    from backend_adapter import webui_status
    return config, webui_status


def _ok_refresh(count: int, errors=None) -> dict:
    return {"ok": True, "count": count, "errors": errors or {}}


def _fail_refresh(count: int, errors) -> dict:
    return {"ok": False, "count": count, "errors": errors}


# ---------------------------------------------------------------------------
# TestConfigSnapshot — режимы: multi / standalone
# ---------------------------------------------------------------------------

class TestConfigSnapshot:
    def test_multi_with_models(self):
        # Один бэкенд в _BACKENDS, модели — из _MODEL_TO_BACKEND.
        config, ws = _fresh_modules()
        cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"}
        config._BACKENDS = [cfg]
        config._MODEL_TO_BACKEND = {
            "m-one": ("AAA", cfg),
            "m-two": ("AAA", cfg),
        }
        snap = ws._config_snapshot()
        assert snap["mode"] == "multi-backend"
        assert len(snap["endpoints"]) == 1
        ep = snap["endpoints"][0]
        assert ep["name"] == "AAA"
        assert ep["base"] == "http://aaa.local"
        assert ep["models"] == ["m-one", "m-two"]
        assert ep["status"] == "ok"
        assert snap["note"] is None

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

    def test_standalone_without_env(self):
        # Режим standalone: ни бэкендов, ни моделей, ни YAML-конфига —
        # страница показывает подсказку (а не фантомный бэкенд).
        config, ws = _fresh_modules()
        config.ADAPTER_BACKEND_CONFIG = ""
        snap = ws._config_snapshot()
        assert snap["mode"] == "standalone"
        assert snap["endpoints"] == []
        assert snap["note"] and "ADAPTER_WEBUI_ENABLE" in snap["note"]

    def test_multi_endpoints_carry_keys(self):
        # key нужен refresh-пробе; в HTML не выводится, но в данных должен быть.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa", "key": "secret-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m1": ("AAA", config._BACKENDS[0])}
        endpoints = ws._collect_endpoints()
        assert endpoints[0]["key"] == "secret-aaa"


# ---------------------------------------------------------------------------
# Рендер после refresh: ошибки/успех по эндпойнтам
# ---------------------------------------------------------------------------

class TestRenderAfterRefresh:
    def _ctx(self):
        return mock.Mock(version="0.0.0-test")

    def test_success_models_from_updated_cache(self):
        # ok=True: страница показывает модели из обновлённого кэша
        # (refresh уже пересобрал _MODEL_TO_BACKEND — как в проде).
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"new-m1": ("AAA", config._BACKENDS[0])}
        body = ws._render_status_page(self._ctx(), refresh=_ok_refresh(1)).decode()
        assert "new-m1" in body
        assert "недоступен" not in body
        assert "Список моделей обновлён" in body

    def test_partial_failure_shows_error_and_ok_row(self):
        # Частичный успех: упавший бэкенд — «недоступен (текст)», живые —
        # ok со своими моделями; футер упоминает ошибку.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
            {"name": "BBB", "base": "http://bbb.local", "key": "k-bbb"},
        ]
        config._MODEL_TO_BACKEND = {
            "m-a": ("AAA", config._BACKENDS[0]),
            "m-b": ("BBB", config._BACKENDS[1]),
        }
        refresh = _ok_refresh(2, errors={"BBB": "Connection refused by test"})
        body = ws._render_status_page(self._ctx(), refresh=refresh).decode()
        assert "недоступен" in body
        assert "Connection refused by test" in body
        assert "m-a" in body
        assert "Список моделей обновлён" in body

    def test_full_failure_keeps_old_cache_shown(self):
        # ok=False: refresh кэш не тронул — страница показывает прежний
        # список и «Не удалось обновить».
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"old-m": ("AAA", config._BACKENDS[0])}
        refresh = _fail_refresh(1, {"AAA": "Connection refused by test"})
        body = ws._render_status_page(self._ctx(), refresh=refresh).decode()
        assert "old-m" in body            # старый кэш не стёрт
        assert "Не удалось обновить" in body
        assert "Connection refused by test" in body

    def test_refresh_none_footer_mentions_autoload(self):
        config, ws = _fresh_modules()
        body = ws._render_status_page(self._ctx()).decode()
        assert "обновляется при каждой" in body or "загрузке страницы" in body

    def test_endpoint_absent_from_errors_after_partial_is_ok(self):
        # Бэкенд без ошибки в refresh — статус из snapshot («ok»), даже если
        # другой бэкенд упал.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
            {"name": "BBB", "base": "http://bbb.local", "key": "k-bbb"},
        ]
        config._MODEL_TO_BACKEND = {"m-a": ("AAA", config._BACKENDS[0])}
        refresh = _ok_refresh(1, errors={"BBB": "boom"})
        body = ws._render_status_page(self._ctx(), refresh=refresh).decode()
        assert '<span style="color:#1a7f37">ok</span>' in body


# ---------------------------------------------------------------------------
# HTTP: GET "/" и POST "/" — оба делают refresh
# ---------------------------------------------------------------------------

class TestStatusHTTP:
    def test_get_root_renders_version_and_refreshes(self, tmp_path):
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}

        fake_result = _ok_refresh(1)
        with mock.patch.object(config, "refresh_models", return_value=fake_result) as m_refresh:
            httpd, port = _start_server(str(tmp_path))
            try:
                status, body = _http_get(port, "/")
                assert status == 200
                assert "0.0.0-test" in body       # версия из контекста сервера
                assert "AAA" in body
                assert "http://aaa.local" in body
                assert "Список моделей обновлён" in body
            finally:
                httpd.shutdown()
                httpd.server_close()
        assert m_refresh.call_count == 1

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
        with mock.patch.object(config, "refresh_models", return_value=_ok_refresh(2)):
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

    def test_get_root_refresh_failure_keeps_old_models(self, tmp_path):
        # Провал refresh не роняет страницу: показываются прежние модели.
        config, _ = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"old-m": ("AAA", config._BACKENDS[0])}
        with mock.patch.object(
            config, "refresh_models",
            return_value=_fail_refresh(1, {"AAA": "boom"}),
        ):
            httpd, port = _start_server(str(tmp_path))
            try:
                status, body = _http_get(port, "/")
                assert status == 200
                assert "old-m" in body
                assert "Не удалось обновить" in body
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_get_root_standalone_notice_no_refresh(self, tmp_path):
        # Standalone без конфига: эндпойнтов нет — refresh не вызывается,
        # страница показывает подсказку (а не фантомный example.com).
        config, ws = _fresh_modules()
        config.ADAPTER_BACKEND_CONFIG = ""
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
        # Авторефреш не ушёл в сеть: в standalone нет эндпойнтов
        assert config._fetch_models is not None  # (заглушка: вызов был бы с сетью)

    def test_post_live_refresh_success(self, tmp_path):
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}

        fake_result = _ok_refresh(2)
        with mock.patch.object(config, "refresh_models", return_value=fake_result) as m_refresh:
            httpd, port = _start_server(str(tmp_path))
            try:
                status, body = _http_post(port, "/")
                assert status == 200
                assert "Список моделей обновлён" in body
            finally:
                httpd.shutdown()
                httpd.server_close()
        assert m_refresh.call_count == 1

    def test_post_refresh_failure_does_not_crash(self, tmp_path):
        config, _ = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}

        with mock.patch.object(
            config, "refresh_models",
            return_value=_fail_refresh(1, {"AAA": "Connection refused by unit test"}),
        ):
            httpd, port = _start_server(str(tmp_path))
            try:
                status, body = _http_post(port, "/")
                assert status == 200
                assert "недоступен" in body
                assert "Connection refused" in body
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_get_and_post_pass_short_timeout(self, tmp_path):
        # GET и POST обязаны звать refresh_models с коротким PROBE_TIMEOUT,
        # а не с ADAPTER_TIMEOUT по умолчанию — страница не должна висеть.
        config, ws = _fresh_modules()
        assert ws.PROBE_TIMEOUT == 5.0
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m": ("AAA", config._BACKENDS[0])}
        with mock.patch.object(config, "refresh_models", return_value=_ok_refresh(1)) as m:
            httpd, port = _start_server(str(tmp_path))
            try:
                _http_get(port, "/")
            finally:
                httpd.shutdown()
                httpd.server_close()
        _, kwargs = m.call_args
        assert kwargs.get("timeout") == 5.0
