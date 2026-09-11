#!/usr/bin/env python3
"""Unit + HTTP tests for backend_adapter.webui_config_api — "/config" endpoint.

Tests cover:
  - Unit: _render_config_page returns HTML with current values
  - HTTP GET /config → 200, HTML with form (13 fields: 6 bool + 3 int +
    1 text (ADAPTER_MODELS_MAPPING) + 3 TARGET select)
  - HTTP POST /config → applies valid, ignores invalid; страница сообщает
    только об ошибках (плашки «Применено» нет)
  - ADAPTER_MODELS_MAPPING правится на лету: config._MAP перестраивается
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

        # Check all keys are present
        for key in config.RUNTIME_CONFIG_POOL:
            assert key in html

        # Check values (v0.8.6: ADAPTER_DEBUG дефолт 0 — файловая запись; другие варьируются)
        assert "ADAPTER_DEBUG" in html

    def test_renders_target_selects(self):
        """TARGET-переменные рендерятся выпадающими списками с текущим значением.

        v0.9.1: три ADAPTER_*_TARGET в форме /config — <select> с доменом
        config.TARGET_ALLOWED_VALUES; текущее значение отмечено selected."""
        from backend_adapter import config
        current = config.get_runtime_config()
        html = self.render(current).decode("utf-8")

        for name in (
            "ADAPTER_MESSAGES_TARGET",
            "ADAPTER_COMPLETIONS_TARGET",
            "ADAPTER_RESPONSES_TARGET",
        ):
            # select присутствует с именем поля
            assert f'<select id="{name}" name="{name}"' in html
            # все допустимые значения — опциями
            for val in config.TARGET_ALLOWED_VALUES:
                assert f'value="{val}"' in html
        # Дефолты: MESSAGES=completions, COMPLETIONS/RESPONSES=none — selected.
        assert '<option value="completions" selected>completions</option>' in html
        assert html.count('<option value="none" selected>none</option>') == 2

    def test_renders_only_errors_no_applied_flash(self):
        """Flash показывает ТОЛЬКО ошибки; плашка «Применено» убрана (v0.9.3).

        Успешное применение не сообщается — обновлённые значения видны в
        колонке «текущее». Если ошибок нет — flash пуст."""
        from backend_adapter import config
        current = config.get_runtime_config()
        applied = {"ignored": ["UNKNOWN_KEY"]}
        html = self.render(current, applied=applied).decode("utf-8")
        assert "Применено" not in html
        assert "Игнорировано" in html
        assert "UNKNOWN_KEY" in html

        # Пустой ignored (всё применилось) — никакого flash вовсе.
        html_ok = self.render(current, applied={"ignored": []}).decode("utf-8")
        assert "Применено" not in html_ok
        assert "Игнорировано" not in html_ok

    def test_renders_mapping_text_field(self):
        """ADAPTER_MODELS_MAPPING рендерится текстовым полем с текущим значением."""
        from backend_adapter import config
        config.ADAPTER_MODELS_MAPPING = "a:b,c:d"
        current = config.get_runtime_config()
        html = self.render(current).decode("utf-8")
        assert (
            '<input type="text" id="ADAPTER_MODELS_MAPPING" '
            'name="ADAPTER_MODELS_MAPPING" value="a:b,c:d"' in html
        )

    def test_head_has_favicon_link(self):
        """В <head> страницы /config есть <link rel="icon" ...> — иконка
        вкладки общая для всех страниц WEBUI (эндпоинт /favicon.svg в ядре)."""
        from backend_adapter import config
        current = config.get_runtime_config()
        html = self.render(current).decode("utf-8")
        assert (
            '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in html
        ), "нет favicon-link в <head> страницы /config"


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
            # Form with 13 fields (6 bool + 3 int + 1 text (маппинг моделей)
            # + 3 select для TARGET)
            assert "ADAPTER_DEBUG" in body
            assert "ADAPTER_DEBUG_PARTS" in body
            assert "ADAPTER_SENSITIVE_LOGGING_ENABLE" in body
            assert "ADAPTER_STREAMING_ENABLE" in body
            assert "ADAPTER_STREAM_INCLUDE_USAGE" in body
            assert "ADAPTER_STRICT_MODELS" in body
            assert "ADAPTER_TRACE_REASONING_MAX_CHARS" in body
            assert "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS" in body
            assert "ADAPTER_DEBUG_TRIM" in body
            # Ровно одно text-поле — маппинг моделей (v0.9.3)
            assert 'type="text"' in body
            assert body.count('type="text"') == 1
            assert "ADAPTER_MODELS_MAPPING" in body
            # TARGET-маршрутизация входов (v0.9.1): 3 выпадающих списка
            assert "ADAPTER_MESSAGES_TARGET" in body
            assert "ADAPTER_COMPLETIONS_TARGET" in body
            assert "ADAPTER_RESPONSES_TARGET" in body
            assert body.count("<select") == 3
            # Favicon — общий ресурс всех страниц WEBUI (см. /favicon.svg)
            assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in body
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
            # Плашки «Применено» больше нет (v0.9.3); значения видны в форме.
            assert "Применено" not in response_body

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
            body = "ADAPTER_DEBUG_TRIM=not_a_number&ADAPTER_DEBUG_PARTS=1".encode()
            status, response_body = _http_post(
                port, "/config", "application/x-www-form-urlencoded", body
            )
            assert status == 200

            # ADAPTER_DEBUG_TRIM should NOT change (invalid type)
            current = config.get_runtime_config()
            assert current["ADAPTER_DEBUG_TRIM"] == before["ADAPTER_DEBUG_TRIM"]
            # ADAPTER_DEBUG_PARTS should apply (valid bool)
            assert current["ADAPTER_DEBUG_PARTS"] is True
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
        before_logpath = config.ADAPTER_DEBUG_LOGPATH

        httpd, port = _start_server(str(tmp_path))
        try:
            body = json.dumps({
                "ADAPTER_DEBUG_LOGPATH": "/tmp/other",  # outside pool
                "ADAPTER_DEBUG": True,
            }).encode()
            status, response_body = _http_post(
                port, "/config", "application/json", body
            )
            assert status == 200

            # LOGPATH (точка хранения) should NOT change
            assert config.ADAPTER_DEBUG_LOGPATH == before_logpath
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_new_bool_fields_applied(self, tmp_path):
        """/config POST (JSON) applies the 4 new bool flags (v0.8.3)."""
        _reload_config()
        from backend_adapter import config

        httpd, port = _start_server(str(tmp_path))
        try:
            body = json.dumps({
                "ADAPTER_SENSITIVE_LOGGING_ENABLE": True,
                "ADAPTER_STREAMING_ENABLE": False,
                "ADAPTER_STREAM_INCLUDE_USAGE": False,
                "ADAPTER_STRICT_MODELS": False,
            }).encode()
            status, response_body = _http_post(
                port, "/config", "application/json", body
            )
            assert status == 200
            current = config.get_runtime_config()
            assert current["ADAPTER_SENSITIVE_LOGGING_ENABLE"] is True
            assert current["ADAPTER_STREAMING_ENABLE"] is False
            assert current["ADAPTER_STREAM_INCLUDE_USAGE"] is False
            assert current["ADAPTER_STRICT_MODELS"] is False
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_parts_checkbox_off_and_on(self, tmp_path):
        """/config: чекбокс ADAPTER_DEBUG_PARTS переключается (bool).

        Форма шлёт для каждого bool-поля пару значений: явный checkbox
        (value=1, только когда отмечен) + hidden-поле "_<NAME>" с состоянием
        1/0 — снятая галка без hidden-«соседа» просто отсутствовала бы в
        теле POST и выключить bool было бы невозможно. В разборе берётся
        последнее значение ключа (hidden), ключ "_NAME" вносится как "NAME"."""
        _reload_config()
        from backend_adapter import config
        config.ADAPTER_DEBUG_PARTS = True  # пред-условие «включено»

        httpd, port = _start_server(str(tmp_path))
        try:
            # Снятая галка: checkbox отсутствует, hidden-состояние → 0
            body = "_ADAPTER_DEBUG_PARTS=0".encode()
            status, response_body = _http_post(
                port, "/config", "application/x-www-form-urlencoded", body
            )
            assert status == 200
            assert config.ADAPTER_DEBUG_PARTS is False

            # Отмеченная галка: checkbox value=1 + hidden-состояние → 1
            body = "ADAPTER_DEBUG_PARTS=1&_ADAPTER_DEBUG_PARTS=1".encode()
            status, response_body = _http_post(
                port, "/config", "application/x-www-form-urlencoded", body
            )
            assert status == 200
            assert config.ADAPTER_DEBUG_PARTS is True
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_target_select_applies(self, tmp_path):
        """/config POST (form-data): select TARGET применяет значение на лету.

        v0.9.1: ADAPTER_*_TARGET входят в runtime-пул — выпадающий список
        шлёт строку из домена, set_runtime_config переприсваивает глобал
        config, routing.target_for_input видит новое значение сразу."""
        _reload_config()
        from backend_adapter import config

        httpd, port = _start_server(str(tmp_path))
        try:
            body = "ADAPTER_COMPLETIONS_TARGET=completions".encode()
            status, response_body = _http_post(
                port, "/config", "application/x-www-form-urlencoded", body
            )
            assert status == 200
            # Значение применилось к модульному глобалу и видно в пуле.
            assert config.ADAPTER_COMPLETIONS_TARGET == "completions"
            current = config.get_runtime_config()
            assert current["ADAPTER_COMPLETIONS_TARGET"] == "completions"
            # Маршрутизатор читает глобал на каждый вызов — смена видна сразу.
            from backend_adapter import routing
            assert routing.target_for_input("completions") == "completions"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_target_invalid_ignored(self, tmp_path):
        """/config POST: невалидное значение TARGET игнорируется, соседний ключ — нет."""
        _reload_config()
        from backend_adapter import config
        before = config.ADAPTER_MESSAGES_TARGET

        httpd, port = _start_server(str(tmp_path))
        try:
            body = "ADAPTER_MESSAGES_TARGET=bogus&ADAPTER_DEBUG=1".encode()
            status, response_body = _http_post(
                port, "/config", "application/x-www-form-urlencoded", body
            )
            assert status == 200
            # TARGET не изменился (не из домена), bool применился.
            assert config.ADAPTER_MESSAGES_TARGET == before
            assert config.ADAPTER_DEBUG is True
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_target_json_applies(self, tmp_path):
        """/config POST (JSON): TARGET меняется на лету."""
        _reload_config()
        from backend_adapter import config

        httpd, port = _start_server(str(tmp_path))
        try:
            body = json.dumps({"ADAPTER_MESSAGES_TARGET": "responses"}).encode()
            status, response_body = _http_post(
                port, "/config", "application/json", body
            )
            assert status == 200
            assert config.ADAPTER_MESSAGES_TARGET == "responses"
            assert config.get_runtime_config()["ADAPTER_MESSAGES_TARGET"] == "responses"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_mapping_applied_live(self, tmp_path):
        """/config POST (form): ADAPTER_MODELS_MAPPING правится на лету.

        v0.9.3: строка маппинга входит в runtime-пул; set_runtime_config
        перестраивает config._MAP НА МЕСТЕ (server.py держит ссылку на
        словарь через `from .config import _MAP`) — новый маппинг виден
        следующему же запросу без перезапуска."""
        _reload_config()
        from backend_adapter import config
        # Ссылка, как её держит server.py: должна обновиться на месте.
        from backend_adapter.config import _MAP as ref
        assert ref is config._MAP

        httpd, port = _start_server(str(tmp_path))
        try:
            body = "ADAPTER_MODELS_MAPPING=a:b,c:d".encode()
            status, response_body = _http_post(
                port, "/config", "application/x-www-form-urlencoded", body
            )
            assert status == 200
            assert "Применено" not in response_body
            assert config.ADAPTER_MODELS_MAPPING == "a:b,c:d"
            assert config.get_runtime_config()["ADAPTER_MODELS_MAPPING"] == "a:b,c:d"
            # Словарь _MAP перестроен на месте — та же ссылка видит новый маппинг.
            assert dict(ref) == {"a": "b", "c": "d"}
            assert dict(config._MAP) == {"a": "b", "c": "d"}
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_mapping_empty_disables(self, tmp_path):
        """/config POST: пустая строка маппинга валидна — _MAP очищается."""
        _reload_config()
        from backend_adapter import config
        config.ADAPTER_MODELS_MAPPING = "a:b"
        config._MAP.clear()
        config._MAP.update({"a": "b"})

        httpd, port = _start_server(str(tmp_path))
        try:
            body = "ADAPTER_MODELS_MAPPING=".encode()
            status, _ = _http_post(
                port, "/config", "application/x-www-form-urlencoded", body
            )
            assert status == 200
            assert config.ADAPTER_MODELS_MAPPING == ""
            assert dict(config._MAP) == {}
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_mapping_json_applies(self, tmp_path):
        """/config POST (JSON): строка маппинга применяется как есть."""
        _reload_config()
        from backend_adapter import config

        httpd, port = _start_server(str(tmp_path))
        try:
            body = json.dumps({"ADAPTER_MODELS_MAPPING": "x:y"}).encode()
            status, _ = _http_post(port, "/config", "application/json", body)
            assert status == 200
            assert config.ADAPTER_MODELS_MAPPING == "x:y"
            assert dict(config._MAP) == {"x": "y"}
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_mapping_wrong_type_ignored(self, tmp_path):
        """/config POST (JSON): не-строка для маппинга игнорируется, сосед — нет."""
        _reload_config()
        from backend_adapter import config
        before = config.ADAPTER_MODELS_MAPPING

        httpd, port = _start_server(str(tmp_path))
        try:
            body = json.dumps({
                "ADAPTER_MODELS_MAPPING": 123,  # не строка
                "ADAPTER_DEBUG": True,
            }).encode()
            status, response_body = _http_post(
                port, "/config", "application/json", body
            )
            assert status == 200
            assert config.ADAPTER_MODELS_MAPPING == before  # не изменился
            assert "ADAPTER_MODELS_MAPPING" in response_body  # в «Игнорировано»
            assert "Игнорировано" in response_body
            assert config.ADAPTER_DEBUG is True  # соседний ключ применился
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_form_zero_int_applied(self, tmp_path):
        """/config POST (form): int-поле со значением «0» применяется (регресс v0.9.1).

        Браузер шлёт int-поля (type="number") строками; «0» раньше кралась
        bool-эвристикой парсера ("0" → False), и set_runtime_config отклонял
        bool для int-поля — лимиты с дефолтом 0 (ADAPTER_TRACE_*_MAX_CHARS,
        ADAPTER_DEBUG_TRIM=0) уходили в «Игнорировано». Типизированный разбор
        (по config._RUNTIME_CONFIG_TYPES) разбирает int-поля через int()
        без bool-эвристики: "0" → 0."""
        _reload_config()
        from backend_adapter import config
        # Пред-условие: лимиты НЕ 0, чтобы применение «0» было наблюдаемым.
        config.ADAPTER_TRACE_REASONING_MAX_CHARS = 100
        config.ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS = 200
        config.ADAPTER_DEBUG_TRIM = 300

        httpd, port = _start_server(str(tmp_path))
        try:
            # Ровно то, что шлёт форма при применении целиком (значения 0).
            body = (
                "ADAPTER_TRACE_REASONING_MAX_CHARS=0"
                "&ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS=0"
                "&ADAPTER_DEBUG_TRIM=0"
            ).encode()
            status, response_body = _http_post(
                port, "/config", "application/x-www-form-urlencoded", body
            )
            assert status == 200
            # Все три int-поля применились как 0 — в «Игнорировано» не ушли.
            assert config.ADAPTER_TRACE_REASONING_MAX_CHARS == 0
            assert config.ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS == 0
            assert config.ADAPTER_DEBUG_TRIM == 0
            # Ошибок нет → flash пуст (и плашки «Применено» тоже нет).
            assert "Игнорировано" not in response_body
            assert "Применено" not in response_body
        finally:
            httpd.shutdown()
            httpd.server_close()


__all__ = [
    "TestRenderConfigPage",
    "TestConfigHTTPGet",
    "TestConfigHTTPPost",
]
