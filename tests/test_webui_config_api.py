#!/usr/bin/env python3
"""Unit + HTTP tests for backend_adapter.webui_config_api — "/config" endpoint.

Tests cover:
  - Unit: _render_config_page returns HTML with current values
  - HTTP GET /config → 200, HTML with form (7 fields)
  - HTTP POST /config → applies valid, ignores invalid, redirects with message
"""
import os
import sys
import json
import threading
import time

import pytest


def _reload_config():
    """Remove backend_adapter modules from sys.modules and reimport."""
    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]


def _start_server(root_dir: str):
    from backend_adapter import webserver
    httpd = webserver.serve(root_dir, "0.0.0-test", port=0)
    assert httpd is not None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_port


def _http_get(port: int, path: str):
    """Raw GET via socket; returns (status, body)."""
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


def _http_post(port: int, path: str, content_type: str, body: bytes):
    """Raw POST via socket; returns (status, body)."""
    import socket
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        request = f"POST {path} HTTP/1.0\r\nHost: localhost\r\nContent-Length: {len(body)}\r\nContent-Type: {content_type}\r\n\r\n".encode()
        sock.sendall(request + body)
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


# ---------------------------------------------------------------------------
# Unit tests: pure logic
# ---------------------------------------------------------------------------

class TestRenderConfigPage:
    def setup_method(self):
        _reload_config()
        from backend_adapter.webui_config_api import _render_config_page
        self.render = _render_config_page

    def test_renders_current_values(self):
        """HTML contains current values from get_runtime_config()."""
        from backend_adapter import config
        current = config.get_runtime_config()
        html_bytes = self.render(current)
        html = html_bytes.decode("utf-8")

        # Check all 7 keys are present
        for key in config.RUNTIME_CONFIG_POOL:
            assert key in html

        # Check values (default: ADAPTER_DEBUG=True, others vary)
        assert "ADAPTER_DEBUG" in html

    def test_renders_with_applied_message(self):
        """HTML contains flash message when applied dict provided."""
        from backend_adapter import config
        current = config.get_runtime_config()
        applied = {"ok": ["ADAPTER_DEBUG"], "ignored": ["UNKNOWN_KEY"]}
        html_bytes = self.render(current, applied=applied)
        html = html_bytes.decode("utf-8")

        assert "Применено:" in html
        assert "ADAPTER_DEBUG" in html
        assert "Игнорировано" in html
        assert "UNKNOWN_KEY" in html


# ---------------------------------------------------------------------------
# HTTP tests: GET /config
# ---------------------------------------------------------------------------

class TestConfigHTTPGet:
    def test_get_config_returns_200_html(self, tmp_path):
        """/config GET → 200, HTML with form."""
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/config")
            assert status == 200
            assert "<!DOCTYPE html>" in body or "<html" in body.lower()
            # Form with 7 fields
            assert "ADAPTER_DEBUG" in body
            assert "ADAPTER_DEBUG_TAGS_OUT" in body
            assert "ADAPTER_DEBUG_TOOLS" in body
            assert "ADAPTER_DEBUG_TOOLS_ERROR" in body
            assert "ADAPTER_TRACE_REASONING_MAX_CHARS" in body
            assert "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS" in body
            assert "ADAPTER_DEBUG_TRIM" in body
        finally:
            httpd.shutdown()
            httpd.server_close()


# ---------------------------------------------------------------------------
# HTTP tests: POST /config
# ---------------------------------------------------------------------------

class TestConfigHTTPPost:
    def test_post_form_data_applies_valid(self, tmp_path):
        """/config POST (form-data) applies valid bool/int values."""
        _reload_config()
        from backend_adapter import config
        # Set initial values
        config.ADAPTER_DEBUG = True
        config.ADAPTER_DEBUG_TRIM = 3000

        httpd, port = _start_server(str(tmp_path))
        try:
            # POST form-data
            body = "ADAPTER_DEBUG=0&ADAPTER_DEBUG_TRIM=1000".encode()
            status, response_body = _http_post(
                port, "/config", "application/x-www-form-urlencoded", body
            )
            assert status == 200
            # Should contain success message
            assert "Применено:" in response_body or "ADAPTER_DEBUG" in response_body

            # Check values actually changed
            current = config.get_runtime_config()
            assert current["ADAPTER_DEBUG"] is False
            assert current["ADAPTER_DEBUG_TRIM"] == 1000
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_form_data_ignores_invalid(self, tmp_path):
        """/config POST (form-data) silently ignores invalid types."""
        _reload_config()
        from backend_adapter import config
        before = config.get_runtime_config()

        httpd, port = _start_server(str(tmp_path))
        try:
            # POST with wrong type (bool as string for int field)
            body = "ADAPTER_DEBUG_TRIM=not_a_number&ADAPTER_DEBUG_TAGS_OUT=1".encode()
            status, response_body = _http_post(
                port, "/config", "application/x-www-form-urlencoded", body
            )
            assert status == 200

            # ADAPTER_DEBUG_TRIM should NOT change (invalid type)
            current = config.get_runtime_config()
            assert current["ADAPTER_DEBUG_TRIM"] == before["ADAPTER_DEBUG_TRIM"]
            # ADAPTER_DEBUG_TAGS_OUT should apply (valid bool)
            assert current["ADAPTER_DEBUG_TAGS_OUT"] is True
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_json_applies_valid(self, tmp_path):
        """/config POST (JSON) applies valid values."""
        _reload_config()
        from backend_adapter import config

        httpd, port = _start_server(str(tmp_path))
        try:
            body = json.dumps({
                "ADAPTER_DEBUG": False,
                "ADAPTER_TRACE_REASONING_MAX_CHARS": 500,
            }).encode()
            status, response_body = _http_post(
                port, "/config", "application/json", body
            )
            assert status == 200

            current = config.get_runtime_config()
            assert current["ADAPTER_DEBUG"] is False
            assert current["ADAPTER_TRACE_REASONING_MAX_CHARS"] == 500
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_unknown_keys_ignored(self, tmp_path):
        """/config POST silently ignores unknown keys."""
        _reload_config()
        from backend_adapter import config
        before = config.get_runtime_config()

        httpd, port = _start_server(str(tmp_path))
        try:
            body = json.dumps({
                "UNKNOWN_KEY": "value",
                "ADAPTER_DEBUG": False,
            }).encode()
            status, response_body = _http_post(
                port, "/config", "application/json", body
            )
            assert status == 200

            # Only ADAPTER_DEBUG should be in response
            current = config.get_runtime_config()
            assert current["ADAPTER_DEBUG"] is False
            assert "UNKNOWN_KEY" not in current
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_key_outside_pool_ignored(self, tmp_path):
        """/config POST ignores keys outside RUNTIME_CONFIG_POOL."""
        _reload_config()
        from backend_adapter import config
        before_webui = config.ADAPTER_WEBUI_ENABLE

        httpd, port = _start_server(str(tmp_path))
        try:
            body = json.dumps({
                "ADAPTER_WEBUI_ENABLE": False,  # outside pool
                "ADAPTER_DEBUG": True,
            }).encode()
            status, response_body = _http_post(
                port, "/config", "application/json", body
            )
            assert status == 200

            # WEBUI_ENABLE should NOT change
            assert config.ADAPTER_WEBUI_ENABLE == before_webui
        finally:
            httpd.shutdown()
            httpd.server_close()


__all__ = [
    "TestRenderConfigPage",
    "TestConfigHTTPGet",
    "TestConfigHTTPPost",
]
