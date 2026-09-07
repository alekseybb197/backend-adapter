#!/usr/bin/env python3
"""Unit + HTTP tests for backend_adapter.prometheus_exporter (v0.8.5).

Отдельный слушатель на ADAPTER_EXPORTER_PORT: GET /metrics (и /) отдаёт
текст text exposition 0.0.4 из живых конфиг-глобалов — настройки/статус
приложения (info/build/uptime/backends_configured/models_available),
по бэкенду backend_up/backend_models/backend_endpoint (из
config._ENDPOINT_STATE — единый источник после синхронизации found-эндпоинтов
модели), по использованной модели счётчики calls/input_tokens/output_tokens
из model_usage.usage_snapshot(). Прочие пути — 404; label-значения
экранируются (\\, ", перевод строки).
"""
import socket
import threading
from unittest import mock

import pytest


def _fresh():
    """Reload config + model_usage with clean state (conftest defaults)."""
    import sys

    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]
    from backend_adapter import config, model_usage

    model_usage.set_persist_path("")
    return config, model_usage


def _backend(name: str = "AAA", base: str = "http://aaa.local") -> dict:
    return {"name": name, "base": base, "key": "k"}


def _seed_state(config):
    """Засеять бэкенды/модели/пробы/usage-таблицу (без сети)."""
    b1 = _backend("AAA", "http://aaa.local")
    b2 = _backend('BB"B', 'http://bb"b.local')
    config._BACKENDS = [b1, b2]
    config._BACKEND_BY_NAME = {b1["name"]: b1, b2["name"]: b2}
    config._MODEL_TO_BACKEND = {
        "m1": (b1["name"], b1),
        "m2": (b1["name"], b1),
        "m3": (b2["name"], b2),
    }
    config._AVAILABLE_MODELS = {"m1": {"id": "m1"}, "m2": {"id": "m2"}, "m3": {"id": "m3"}}
    # Проба эндпоинтов: у AAA — completions 200 (found), у BB"B — ничего.
    config._ENDPOINT_STATE["AAA"] = {
        "at": 1.0,
        "endpoints": {"/v1/chat/completions": {"status": 200, "found": True}},
        "errors": {},
    }
    # Последняя проверка бэкендов: AAA упал на /v1/models (есть в errors).
    config._REFRESH_JOB = {
        "running": False,
        "started_at": 1.0,
        "done_at": 2.0,
        "ok": False,
        "count": 2,
        "providers": 2,
        "errors": {"AAA": "Connection refused"},
        "checked_at": "10:00:00",
    }
    return b1, b2


def _seed_usage(model_usage):
    """Засеять строки таблицы Models in use (модель с кавычкой в имени)."""
    now = __import__("time").strftime("%H:%M:%S")
    model_usage._TABLE["m1"] = {
        "model": "m1",
        "backend": "AAA",
        "calls": 7,
        "input_tokens": 300,
        "output_tokens": 500,
        "endpoints": {},
        "errors": {},
        "first_seen": now,
        "probing": False,
    }
    model_usage._TABLE['weird"model'] = {
        "model": 'weird"model',
        "backend": "AAA",
        "calls": 1,
        "input_tokens": 10,
        "output_tokens": 20,
        "endpoints": {},
        "errors": {},
        "first_seen": now,
        "probing": False,
    }
    model_usage._DIRTY = True


def _start_exporter(version: str = "0.0.0-test", host: str = "127.0.0.1"):
    """Экспортёр на эфемерном порту; возвращает (httpd, port)."""
    from backend_adapter import prometheus_exporter as pe

    httpd = pe.serve_exporter(version, host, port=0, verbose=False)
    assert httpd is not None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_port


def _http_get(port: int, path: str):
    """Сырой GET: (status, headers: dict, body_text)."""
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
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return status, headers, body
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Unit: _label_escape
# ---------------------------------------------------------------------------

class TestLabelEscape:
    def test_escapes_backslash_quote_newline(self):
        from backend_adapter import prometheus_exporter as pe

        assert pe._label_escape('a"b\\c\nd') == 'a\\"b\\\\c\\nd'
        assert pe._label_escape("plain") == "plain"


# ---------------------------------------------------------------------------
# Unit: _render_metrics
# ---------------------------------------------------------------------------

class TestRenderMetrics:
    def test_app_level_groups_present(self):
        """info/build/uptime/backends_configured/models_available — gauge-блоки
        с корректными значениями (uptime ≥ 0, бэкенды/модели из глобалов)."""
        config, mu = _fresh()
        _seed_state(config)
        from backend_adapter import prometheus_exporter as pe

        pe.MetricsHandler.version = "0.0.0-test"
        text = pe._render_metrics()
        assert '# HELP backend_adapter_info' in text
        assert '# TYPE backend_adapter_info gauge' in text
        assert 'backend_adapter_info{version="0.0.0-test"}' in text
        assert 'backend_adapter_build_info{version="0.0.0-test"}' in text
        assert '# TYPE backend_adapter_uptime_seconds gauge' in text
        assert 'backend_adapter_backends_configured 2' in text
        assert 'backend_adapter_models_available 3' in text

    def test_backend_metrics_with_state(self):
        """По бэкенду: backend_up (1/0 по refresh-errors), backend_models,
        backend_endpoint по _ENDPOINT_STATE (непробованные пути → 0)."""
        config, mu = _fresh()
        _seed_state(config)
        from backend_adapter import prometheus_exporter as pe

        text = pe._render_metrics()
        # AAA в errors последней проверки → up 0; BB"B не в errors → up 1.
        assert 'backend_adapter_backend_up{name="AAA",base="http://aaa.local"} 0' in text
        assert 'backend_adapter_backend_up{name="BB\\"B",base="http://bb\\"b.local"} 1' in text
        assert 'backend_adapter_backend_models{name="AAA",base="http://aaa.local"} 2' in text
        # endpoint completions у AAA — found (1); остальные непробованные → 0.
        assert (
            'backend_adapter_backend_endpoint{name="AAA",base="http://aaa.local",'
            'endpoint="completions"} 1' in text
        )
        assert (
            'backend_adapter_backend_endpoint{name="AAA",base="http://aaa.local",'
            'endpoint="embeddings"} 0' in text
        )

    def test_usage_model_counter_metrics(self):
        """Счётчики моделей из usage-таблицы (calls/input/output), с label
        model/backend и экранированием кавычек в имени модели."""
        config, mu = _fresh()
        _seed_state(config)
        _seed_usage(mu)
        from backend_adapter import prometheus_exporter as pe

        text = pe._render_metrics()
        assert (
            'backend_adapter_model_calls_total{model="m1",backend="AAA"} 7' in text
        )
        assert (
            'backend_adapter_model_input_tokens_total{model="m1",backend="AAA"} 300' in text
        )
        assert (
            'backend_adapter_model_output_tokens_total{model="m1",backend="AAA"} 500' in text
        )
        # Имя модели с кавычкой экранировано.
        assert (
            'backend_adapter_model_calls_total{model="weird\\"model",backend="AAA"} 1' in text
        )

    def test_empty_state_renders_headers_only(self):
        """Пустые глобалы (тестовый дефолт): группы приложения есть, по
        бэкендам/моделям строк нет — текст корректен."""
        config, mu = _fresh()
        from backend_adapter import prometheus_exporter as pe

        text = pe._render_metrics()
        assert 'backend_adapter_backends_configured 0' in text
        assert 'backend_adapter_models_available 0' in text
        assert "backend_adapter_backend_up{" not in text
        assert "backend_adapter_model_calls_total{" not in text


# ---------------------------------------------------------------------------
# HTTP: отдельный слушатель
# ---------------------------------------------------------------------------

class TestExporterHTTP:
    def test_metrics_200_text_exposition(self):
        """GET /metrics → 200 text/plain; version=0.0.4; charset=utf-8 с
        текстом метрик."""
        config, mu = _fresh()
        _seed_state(config)
        _seed_usage(mu)
        httpd, port = _start_exporter()
        try:
            status, headers, body = _http_get(port, "/metrics")
            assert status == 200
            ct = headers.get("content-type", "")
            assert "text/plain" in ct and "version=0.0.4" in ct
            assert "backend_adapter_info{" in body
            assert "backend_adapter_backend_up{" in body
            assert "backend_adapter_model_calls_total{" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_root_serves_metrics(self):
        """GET / — тот же текст метрик, что /metrics."""
        config, mu = _fresh()
        httpd, port = _start_exporter()
        try:
            status, _h, body = _http_get(port, "/")
            assert status == 200
            assert "backend_adapter_info{" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_unknown_path_404(self):
        """Прочие пути (/nope, /metrics/x) → 404."""
        config, mu = _fresh()
        httpd, port = _start_exporter()
        try:
            for path in ("/nope", "/metrics/x", "/api"):
                status, _h, _b = _http_get(port, path)
                assert status == 404, path
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_version_label_from_serve(self):
        """version в info/build приходит из serve_exporter(version, ...)."""
        config, mu = _fresh()
        _seed_state(config)
        httpd, port = _start_exporter(version="9.9.9-test")
        try:
            _s, _h, body = _http_get(port, "/metrics")
            assert 'backend_adapter_info{version="9.9.9-test"} 1' in body
            assert 'backend_adapter_build_info{version="9.9.9-test"} 1' in body
        finally:
            httpd.shutdown()
            httpd.server_close()
