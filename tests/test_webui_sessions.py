#!/usr/bin/env python3
"""Unit + HTTP tests for backend_adapter.webui_sessions — the "/sessions"
page (v0.9.5) and its per-session control APIs.

До v0.9.5 таблица Sessions жила секцией статус-страницы "/" (тесты были в
test_webui_status.py — см. TestSessionsSection/TestSessionsSnapshotAPI/
TestSessionDeleteAPI в истории). Теперь это ОТДЕЛЬНАЯ страница "/sessions"
и единица УПРАВЛЕНИЯ сессией: пер-сессионные Log/Parts/TARGET (см.
session_settings), ⏪ сброс счётчиков строки, 🗑 удаление строки, ссылка
«Ошибок» на .err-файл (открывается в новом окне).

Покрытие:
  - TestSessionSelectHelpers — чистые хелперы селектов/ячеек;
  - TestSessionsRowsHtml — рендер строк таблицы (13 колонок, data-атрибуты,
    экранирование, селекты, кнопки ⏪/🗑, ссылка .err);
  - TestSessionsPage — GET "/sessions": заголовок, навигация, поллинг;
  - TestSessionsSnapshotAPI — GET /api/sessions/snapshot (+ errors_html);
  - TestSessionResetAPI — POST /api/sessions/reset (PRG/JSON);
  - TestSessionDeleteAPI — POST /api/sessions/delete (PRG/JSON);
  - TestSessionSettingsAPI — POST /api/sessions/settings (форма/JSON);
  - TestSessionLogFileEndpoint — GET /logs/<name> (раздача .err, обход пути).
"""
import json
import os
import socket
import threading
from unittest import mock

# Autouse fresh_env deletes all backend_adapter* modules and re-imports
# config with the default test env. Tests assign config globals directly on
# the *current* module instance (fresh per test — no cross-test pollution).


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
        length = 0
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
            except TimeoutError:
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


def _http_raw(port: int, method: str, path: str):
    """Сырой ответ: (status, headers: dict, body) — для 303/Location-проверок."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        extra = "Content-Length: 0\r\n" if method == "POST" else ""
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
            except TimeoutError:
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


def _http_post_body(port: int, path: str, body: bytes, content_type: str):
    """POST с телом (JSON-клиенты и HTML-формы селектов)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(
            (
                f"POST {path} HTTP/1.0\r\n"
                f"Host: localhost\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode() + body
        )
        response = b""
        while True:
            try:
                sock.settimeout(3)
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            except TimeoutError:
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


def _fresh_modules():
    """Свежие модули webui_sessions + config (после autouse fresh_env)."""
    from backend_adapter import config, webui_sessions
    return config, webui_sessions


def _seed_session_rows(session_registry_mod, rows):
    """Заполнить реестр сессий строками напрямую (сид для рендера).

    Ключ _TABLE — кортеж (session, agent, model, backend, route): одна сессия
    может занимать несколько строк. ``_ts`` задаётся вызывающим — от него
    зависит порядок snapshot (новые сверху). ``input`` (v0.9.5) — входной
    эндпойнт строки (по умолчанию messages)."""
    session_registry_mod._TABLE.clear()
    for r in rows:
        row = {
            "session": r["session"],
            "agent": r.get("agent", ""),
            "input": r.get("input", "messages"),
            "model": r.get("model", ""),
            "backend": r.get("backend", ""),
            "route": r.get("route", ""),
            "last_seen": r.get("last_seen", "2026-09-09 10:00:00"),
            "calls": r.get("calls", 1),
            "errors": r.get("errors", 0),
            "_ts": r.get("_ts", 1.0),
        }
        key = (row["session"], row["agent"], row["model"], row["backend"], row["route"])
        session_registry_mod._TABLE[key] = row


def _row(session, agent="claude-cli/2.1.236", model="m-a", backend="AAA",
         route="passthrough messages→messages", input="messages",
         last_seen="2026-09-09 10:00:00", calls=1, errors=0, ts=1.0):
    """Строка-снимок реестра с полем "key" (JSON-строка кортежа)."""
    from backend_adapter import session_registry
    row = {
        "session": session, "agent": agent, "input": input, "model": model,
        "backend": backend, "route": route, "last_seen": last_seen,
        "calls": calls, "errors": errors, "_ts": ts,
    }
    row["key"] = session_registry.key_json((session, agent, model, backend, route))
    return row


def _ctx():
    return mock.Mock(version="0.0.0-test")


# ---------------------------------------------------------------------------
# TestSessionSelectHelpers — чистые хелперы ячеек/селектов
# ---------------------------------------------------------------------------

class TestSessionSelectHelpers:
    def test_bool_options_marks_inherited_value(self):
        config, ws = _fresh_modules()
        config.ADAPTER_DEBUG = False
        assert ws._bool_options("ADAPTER_DEBUG") == (
            ("inherit", "inherit (off)"), ("1", "on"), ("0", "off"),
        )
        config.ADAPTER_DEBUG = True
        assert ws._bool_options("ADAPTER_DEBUG")[0] == ("inherit", "inherit (on)")

    def test_target_options_include_domain(self):
        config, ws = _fresh_modules()
        config.ADAPTER_MESSAGES_TARGET = "completions"
        opts = ws._target_options("ADAPTER_MESSAGES_TARGET")
        assert opts[0] == ("inherit", "inherit (completions)")
        values = [v for v, _ in opts]
        assert values == ["inherit", *config.TARGET_ALLOWED_VALUES]

    def test_stored_bool_states(self):
        config, ws = _fresh_modules()
        from backend_adapter import session_settings
        assert ws._stored_bool("s-1", "ADAPTER_DEBUG") == "inherit"
        session_settings.set_config("s-1", {"ADAPTER_DEBUG": True})
        assert ws._stored_bool("s-1", "ADAPTER_DEBUG") == "1"
        session_settings.set_config("s-1", {"ADAPTER_DEBUG": False})
        assert ws._stored_bool("s-1", "ADAPTER_DEBUG") == "0"

    def test_input_cell_known_and_unknown(self):
        config, ws = _fresh_modules()
        html = ws._input_cell_html({"input": "messages"})
        assert '<code title="/v1/messages">messages</code>' in html
        assert "—" in ws._input_cell_html({"input": ""})
        assert "—" in ws._input_cell_html({"input": "bogus"})

    def test_errors_cell_plain_number_without_err_file(self):
        # .err-файла нет (session_log ts не назначался) → просто число.
        config, ws = _fresh_modules()
        assert ws._errors_cell_html({"session": "s-1", "errors": 3}) == "3"

    def test_errors_cell_links_err_file(self, tmp_path):
        # Сессия фиксировала ts → число становится ссылкой на /logs/<имя>,
        # открывающейся в НОВОМ окне (target="_blank").
        config, ws = _fresh_modules()
        from backend_adapter import session_log
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._DEBUG_IS_DIR = True
        session_log._session_file_ts["s-1"] = "20260912-101010"
        html = ws._errors_cell_html({"session": "s-1", "errors": 2})
        assert 'href="/logs/session-20260912-101010-s-1.err"' in html
        assert 'target="_blank"' in html
        assert ">2</a>" in html

    def test_actions_cell_has_reset_and_delete(self):
        config, ws = _fresh_modules()
        row = _row("sess-1")
        from urllib.parse import quote
        q = quote(row["key"], safe="")
        html = ws._actions_cell_html(row)
        assert f'action="/api/sessions/reset?key={q}"' in html
        assert f'action="/api/sessions/delete?key={q}"' in html
        assert "⏪" in html and "🗑" in html
        assert 'title="Сбросить счётчики строки сессии"' in html
        assert "color:#c0392b" in html  # удаление — красное

    def test_target_cell_known_and_unknown_input(self):
        config, ws = _fresh_modules()
        config.ADAPTER_MESSAGES_TARGET = "completions"
        html = ws._target_cell_html(_row("s-1", input="messages"))
        # Селект адресует ИМЕННО переменную входа строки.
        assert "name=ADAPTER_MESSAGES_TARGET" in html
        assert 'value="inherit"' in html
        assert "—" in ws._target_cell_html(_row("s-1", input="bogus"))

    def test_target_cell_reflects_override(self):
        config, ws = _fresh_modules()
        from backend_adapter import session_settings
        session_settings.set_config("s-1", {"ADAPTER_MESSAGES_TARGET": "passthrough"})
        html = ws._target_cell_html(_row("s-1", input="messages"))
        assert '<option value="passthrough" selected>' in html


# ---------------------------------------------------------------------------
# TestSessionsRowsHtml — рендер строк таблицы
# ---------------------------------------------------------------------------

class TestSessionsRowsHtml:
    def test_empty_shows_placeholder(self):
        config, ws = _fresh_modules()
        html = ws._sessions_rows_html([])
        assert "пока нет данных" in html
        assert "таблица заполняется при обращениях агентов" in html
        assert 'colspan="13"' in html

    def test_row_has_13_cells_and_data_attrs(self):
        config, ws = _fresh_modules()
        sid = "1ad13437-1111-2222-3333-444455556666"
        row = _row(sid)
        html = ws._sessions_rows_html([row])
        assert html.count("<td") == 13
        assert f'<tr data-session="{sid}" data-key=' in html
        assert f'<code title="{sid}">{sid[:8]}</code>' in html
        assert 'data-calls="1"' in html
        assert 'data-errors="0"' in html

    def test_row_has_log_parts_target_selects(self):
        config, ws = _fresh_modules()
        html = ws._sessions_rows_html([_row("sess-1")])
        assert "name=ADAPTER_DEBUG" in html
        assert "name=ADAPTER_DEBUG_PARTS" in html
        assert "name=ADAPTER_MESSAGES_TARGET" in html
        assert "this.form.submit()" in html

    def test_escapes_session_and_agent(self):
        config, ws = _fresh_modules()
        html = ws._sessions_rows_html([
            _row('<script>alert(1)</script>', agent="a<b>&c"),
        ])
        assert "&lt;script&gt;" in html
        assert "<script>alert" not in html
        assert "a&lt;b&gt;&amp;c" in html

    def test_two_rows_of_same_session(self):
        config, ws = _fresh_modules()
        sid = "1ad13437-1111-2222-3333-444455556666"
        html = ws._sessions_rows_html([
            _row(sid, model="m-a", ts=1.0),
            _row(sid, model="m-b", route="convert messages→completions", ts=2.0),
        ])
        assert html.count(f'data-session="{sid}"') == 2
        assert html.count("data-key=") == 2
        assert ">m-a<" in html and ">m-b<" in html


# ---------------------------------------------------------------------------
# TestSessionsPage — GET "/sessions"
# ---------------------------------------------------------------------------

class TestSessionsPage:
    def test_page_has_headers_and_rows(self, tmp_path):
        config, ws = _fresh_modules()
        from backend_adapter import session_registry
        _seed_session_rows(session_registry, [
            {"session": "s-1", "_ts": 1.0, "model": "m-a",
             "agent": "claude-cli/2.1.236", "backend": "AAA",
             "route": "passthrough messages→messages"},
        ])
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/sessions")
            assert status == 200
            for header in ("Сессия", "Агент", "Модель", "Бэкенд",
                           "Входной эндпойнт", "Маршрут", "Последнее обращение",
                           "Вызовов", "Ошибок", "Log", "Parts", "TARGET",
                           "Actions"):
                assert f"<th>{header}</th>" in body
            assert "s-1" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_page_has_back_links_without_target(self, tmp_path):
        # Обратные ссылки страницы (на статус, обзор сессий, runtime config)
        # ведут в ТЕКУЩЕМ окне (задача 2: target="_blank" — только у ссылок СО
        # статус-страницы и у ссылки на .err-файл).
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/sessions")
            assert status == 200
            assert '<a href="/">Статус 📊</a>' in body
            assert '<a href="/session">Обзор сессий 📋</a>' in body
            assert '<a href="/config">Runtime config 🔧</a>' in body
            assert 'target="_blank"' not in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_page_has_sessions_poll(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/sessions")
            assert status == 200
            assert 'fetch("/api/sessions/snapshot")' in body
            assert 'querySelectorAll("tr[data-key]")' in body
            assert "setTimeout(sessions_poll, 5000)" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_nested_path_is_404(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _ = _http_get(port, "/sessions/extra")
            assert status == 404
        finally:
            httpd.shutdown()
            httpd.server_close()


# ---------------------------------------------------------------------------
# TestSessionsSnapshotAPI — GET /api/sessions/snapshot
# ---------------------------------------------------------------------------

class TestSessionsSnapshotAPI:
    def test_returns_json_with_errors_html(self, tmp_path):
        config, ws = _fresh_modules()
        from backend_adapter import session_registry
        _seed_session_rows(session_registry, [
            {"session": "s-old", "_ts": 1.0, "calls": 2, "errors": 1,
             "agent": "claude-cli/2.1.236", "model": "m-a", "backend": "AAA",
             "route": "passthrough messages→messages"},
            {"session": "s-new", "_ts": 2.0, "calls": 5, "errors": 0,
             "agent": "claude-cli/2.1.236", "model": "m-b", "backend": "AAA",
             "route": "convert messages→completions"},
        ])
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/api/sessions/snapshot")
            assert status == 200
            rows = json.loads(body)
            assert [r["session"] for r in rows] == ["s-new", "s-old"]
            assert rows[0]["calls"] == 5
            assert "_ts" not in rows[0]
            assert rows[0]["errors_html"] == "0"  # .err нет → число
            assert rows[0]["key"] == (
                '["s-new","claude-cli/2.1.236","m-b","AAA",'
                '"convert messages→completions"]'
            )
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_empty_list(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/api/sessions/snapshot")
            assert status == 200
            assert json.loads(body) == []
        finally:
            httpd.shutdown()
            httpd.server_close()


# ---------------------------------------------------------------------------
# TestSessionResetAPI — POST /api/sessions/reset
# ---------------------------------------------------------------------------

class TestSessionResetAPI:
    def _seed_one(self):
        config, ws = _fresh_modules()
        from backend_adapter import session_registry
        _seed_session_rows(session_registry, [
            {"session": "s-1", "_ts": 1.0, "model": "m-a", "calls": 7, "errors": 4,
             "agent": "claude-cli/2.1.236", "backend": "AAA",
             "route": "passthrough messages→messages"},
        ])
        key = session_registry.key_json(
            ("s-1", "claude-cli/2.1.236", "m-a", "AAA",
             "passthrough messages→messages")
        )
        return session_registry, key

    def test_post_form_resets_and_redirects(self, tmp_path):
        from urllib.parse import quote
        session_registry, key = self._seed_one()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, headers, _ = _http_raw(
                port, "POST", "/api/sessions/reset?key=" + quote(key, safe="")
            )
            assert status == 303
            assert headers.get("location") == "/sessions"
            row = session_registry.sessions_snapshot()[0]
            assert row["calls"] == 0 and row["errors"] == 0
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_reset_only_affects_the_row(self, tmp_path):
        # Обнуляется ТОЛЬКО строка-кортеж: соседняя строка той же сессии цела.
        from urllib.parse import quote
        config, ws = _fresh_modules()
        from backend_adapter import session_registry
        _seed_session_rows(session_registry, [
            {"session": "s-1", "_ts": 1.0, "model": "m-a", "calls": 5, "errors": 2,
             "agent": "a", "backend": "AAA", "route": "r-a"},
            {"session": "s-1", "_ts": 2.0, "model": "m-b", "calls": 9, "errors": 3,
             "agent": "a", "backend": "AAA", "route": "r-b"},
        ])
        key = session_registry.key_json(("s-1", "a", "m-a", "AAA", "r-a"))
        httpd, port = _start_server(str(tmp_path))
        try:
            _http_raw(port, "POST", "/api/sessions/reset?key=" + quote(key, safe=""))
            by_model = {r["model"]: r for r in session_registry.sessions_snapshot()}
            assert by_model["m-a"]["calls"] == 0 and by_model["m-a"]["errors"] == 0
            assert by_model["m-b"]["calls"] == 9 and by_model["m-b"]["errors"] == 3
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_json_ok(self, tmp_path):
        from urllib.parse import quote
        session_registry, key = self._seed_one()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _, body = _http_post_body(
                port, "/api/sessions/reset?key=" + quote(key, safe=""),
                b"", "application/json",
            )
            assert status == 200
            assert json.loads(body)["ok"] is True
            assert session_registry.sessions_snapshot()[0]["calls"] == 0
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_json_missing_row_404(self, tmp_path):
        config, ws = _fresh_modules()
        from backend_adapter import session_registry
        _seed_session_rows(session_registry, [])
        key = session_registry.key_json(("ghost", "a", "m", "b", "r"))
        httpd, port = _start_server(str(tmp_path))
        try:
            from urllib.parse import quote
            status, _, body = _http_post_body(
                port, "/api/sessions/reset?key=" + quote(key, safe=""),
                b"", "application/json",
            )
            assert status == 404
            assert "error" in json.loads(body)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_json_bad_key_400(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            for bad in ("not-json", "%5B%22a%22%5D", "%5B1%2C2%2C3%2C4%2C5%5D"):
                status, _, body = _http_post_body(
                    port, "/api/sessions/reset?key=" + bad, b"", "application/json",
                )
                assert status == 400, bad
                assert "error" in json.loads(body)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_get_is_404(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _ = _http_get(port, "/api/sessions/reset")
            assert status == 404
        finally:
            httpd.shutdown()
            httpd.server_close()


# ---------------------------------------------------------------------------
# TestSessionDeleteAPI — POST /api/sessions/delete
# ---------------------------------------------------------------------------

class TestSessionDeleteAPI:
    def _seed_one(self):
        config, ws = _fresh_modules()
        from backend_adapter import session_registry
        _seed_session_rows(session_registry, [
            {"session": "s-1", "_ts": 1.0, "model": "m-a",
             "agent": "claude-cli/2.1.236", "backend": "AAA",
             "route": "passthrough messages→messages"},
        ])
        key = session_registry.key_json(
            ("s-1", "claude-cli/2.1.236", "m-a", "AAA",
             "passthrough messages→messages")
        )
        return session_registry, key

    def test_post_form_deletes_and_redirects(self, tmp_path):
        from urllib.parse import quote
        session_registry, key = self._seed_one()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, headers, _ = _http_raw(
                port, "POST", "/api/sessions/delete?key=" + quote(key, safe="")
            )
            assert status == 303
            assert headers.get("location") == "/sessions"
            assert session_registry.sessions_snapshot() == []
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_json_ok(self, tmp_path):
        from urllib.parse import quote
        session_registry, key = self._seed_one()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _, body = _http_post_body(
                port, "/api/sessions/delete?key=" + quote(key, safe=""),
                b"", "application/json",
            )
            assert status == 200
            assert json.loads(body)["ok"] is True
            assert session_registry.sessions_snapshot() == []
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_json_missing_row_404(self, tmp_path):
        config, ws = _fresh_modules()
        from backend_adapter import session_registry
        _seed_session_rows(session_registry, [])
        key = session_registry.key_json(("ghost", "a", "m", "b", "r"))
        httpd, port = _start_server(str(tmp_path))
        try:
            from urllib.parse import quote
            status, _, body = _http_post_body(
                port, "/api/sessions/delete?key=" + quote(key, safe=""),
                b"", "application/json",
            )
            assert status == 404
            assert "error" in json.loads(body)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_json_missing_key_400(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _, body = _http_post_body(
                port, "/api/sessions/delete", b"", "application/json"
            )
            assert status == 400
            assert "key" in json.loads(body)["error"]
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_form_bad_key_redirects(self, tmp_path):
        config, ws = _fresh_modules()
        from backend_adapter import session_registry
        _seed_session_rows(session_registry, [
            {"session": "s-1", "_ts": 1.0, "model": "m-a"},
        ])
        httpd, port = _start_server(str(tmp_path))
        try:
            status, headers, _ = _http_raw(
                port, "POST", "/api/sessions/delete?key=garbage"
            )
            assert status == 303
            assert headers.get("location") == "/sessions"
            assert len(session_registry.sessions_snapshot()) == 1  # строка цела
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_get_is_404(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _ = _http_get(port, "/api/sessions/delete")
            assert status == 404
        finally:
            httpd.shutdown()
            httpd.server_close()


# ---------------------------------------------------------------------------
# TestSessionSettingsAPI — POST /api/sessions/settings
# ---------------------------------------------------------------------------

class TestSessionSettingsAPI:
    def test_form_sets_bool_and_redirects(self, tmp_path):
        config, ws = _fresh_modules()
        from backend_adapter import session_settings
        httpd, port = _start_server(str(tmp_path))
        try:
            status, headers, _ = _http_post_body(
                port, "/api/sessions/settings?session=s-1&name=ADAPTER_DEBUG",
                b"value=1", "application/x-www-form-urlencoded",
            )
            assert status == 303
            assert headers.get("location") == "/sessions"
            assert session_settings.override("s-1", "ADAPTER_DEBUG") is True
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_form_inherit_clears_bool(self, tmp_path):
        config, ws = _fresh_modules()
        from backend_adapter import session_settings
        session_settings.set_config("s-1", {"ADAPTER_DEBUG": True})
        httpd, port = _start_server(str(tmp_path))
        try:
            _http_post_body(
                port, "/api/sessions/settings?session=s-1&name=ADAPTER_DEBUG",
                b"value=inherit", "application/x-www-form-urlencoded",
            )
            assert session_settings.override("s-1", "ADAPTER_DEBUG") is None
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_form_sets_target_inherit_explicit(self, tmp_path):
        # У TARGET "inherit" — хранимое значение (не снятие записи).
        config, ws = _fresh_modules()
        from backend_adapter import session_settings
        session_settings.set_config("s-1", {"ADAPTER_MESSAGES_TARGET": "passthrough"})
        httpd, port = _start_server(str(tmp_path))
        try:
            _http_post_body(
                port,
                "/api/sessions/settings?session=s-1&name=ADAPTER_MESSAGES_TARGET",
                b"value=inherit", "application/x-www-form-urlencoded",
            )
            assert session_settings.override("s-1", "ADAPTER_MESSAGES_TARGET") == "inherit"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_json_single_setting(self, tmp_path):
        config, ws = _fresh_modules()
        from backend_adapter import session_settings
        httpd, port = _start_server(str(tmp_path))
        try:
            payload = json.dumps(
                {"session": "s-1", "name": "ADAPTER_MESSAGES_TARGET",
                 "value": "passthrough"}
            ).encode()
            status, _, body = _http_post_body(
                port, "/api/sessions/settings", payload, "application/json"
            )
            assert status == 200
            data = json.loads(body)
            assert data["ok"] is True
            assert data["overrides"]["ADAPTER_MESSAGES_TARGET"] == "passthrough"
            assert session_settings.override(
                "s-1", "ADAPTER_MESSAGES_TARGET") == "passthrough"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_json_bulk_values_and_clear(self, tmp_path):
        config, ws = _fresh_modules()
        from backend_adapter import session_settings
        session_settings.set_config("s-1", {"ADAPTER_DEBUG_PARTS": True})
        httpd, port = _start_server(str(tmp_path))
        try:
            payload = json.dumps(
                {"session": "s-1",
                 "values": {"ADAPTER_DEBUG": True, "ADAPTER_COMPLETIONS_TARGET": "none"},
                 "clear": ["ADAPTER_DEBUG_PARTS"]}
            ).encode()
            status, _, body = _http_post_body(
                port, "/api/sessions/settings", payload, "application/json"
            )
            assert status == 200
            overrides = json.loads(body)["overrides"]
            assert overrides["ADAPTER_DEBUG"] is True
            assert overrides["ADAPTER_COMPLETIONS_TARGET"] == "none"
            assert "ADAPTER_DEBUG_PARTS" not in overrides
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_json_missing_session_400(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _, body = _http_post_body(
                port, "/api/sessions/settings",
                json.dumps({"name": "ADAPTER_DEBUG", "value": True}).encode(),
                "application/json",
            )
            assert status == 400
            assert "session" in json.loads(body)["error"]
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_json_invalid_value_400(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _, body = _http_post_body(
                port, "/api/sessions/settings",
                json.dumps({"session": "s-1", "name": "ADAPTER_MESSAGES_TARGET",
                            "value": "bogus"}).encode(),
                "application/json",
            )
            assert status == 400
            assert "error" in json.loads(body)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_json_bad_json_400(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _, body = _http_post_body(
                port, "/api/sessions/settings", b"{not json", "application/json"
            )
            assert status == 400
            assert "error" in json.loads(body)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_get_is_404(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _ = _http_get(port, "/api/sessions/settings")
            assert status == 404
        finally:
            httpd.shutdown()
            httpd.server_close()


# ---------------------------------------------------------------------------
# TestSessionLogFileEndpoint — GET /logs/<name>
# ---------------------------------------------------------------------------

class TestSessionLogFileEndpoint:
    def test_serves_err_file_as_text(self, tmp_path):
        config, ws = _fresh_modules()
        name = "session-20260912-101010-abcd1234.err"
        (tmp_path / name).write_text("ERROR block\n", encoding="utf-8")
        httpd, port = _start_server(str(tmp_path))
        try:
            status, headers, body = _http_raw(port, "GET", "/logs/" + name)
            assert status == 200
            assert "text/plain" in headers.get("content-type", "")
            assert "ERROR block" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_missing_file_404(self, tmp_path):
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _, _ = _http_raw(
                port, "GET", "/logs/session-20260912-101010-abcd1234.err"
            )
            assert status == 404
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_rejects_bad_names(self, tmp_path):
        # Строгий шаблон имени: слэши, «..» и посторонние файлы не отдаются.
        config, ws = _fresh_modules()
        # посторонний файл в корне — не .err сессии
        (tmp_path / "secret.err").write_text("secret", encoding="utf-8")
        (tmp_path / "session-20260912-101010-abcd1234.txt").write_text(
            "x", encoding="utf-8"
        )
        httpd, port = _start_server(str(tmp_path))
        try:
            for bad in (
                "secret.err",
                "session-20260912-101010-abcd1234.txt",
                "session-20260912-101010-abcd1234.err.bak",
                "..%2Fsession-20260912-101010-abcd1234.err",
                "%2Fetc%2Fpasswd",
            ):
                status, _, _ = _http_raw(port, "GET", "/logs/" + bad)
                assert status == 404, bad
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_traversal_via_dotdot_is_404(self, tmp_path):
        # ../ не выходит за корень: после unquote остаются слэши → 404.
        config, ws = _fresh_modules()
        outer = tmp_path.parent / "outside.err"
        outer.write_text("nope", encoding="utf-8")
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _, _ = _http_raw(port, "GET", "/logs/..%2F..%2Foutside.err")
            assert status == 404
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_os_path_check(self, tmp_path):
        # Прямая проверка шаблона имени (без HTTP).
        config, ws = _fresh_modules()
        assert ws._ERR_NAME_RE.match("session-20260912-101010-abcd1234.err")
        assert ws._ERR_NAME_RE.match("session-20260912-101010-s_1.err")
        assert not ws._ERR_NAME_RE.match("../session-20260912-101010-a.err")
        assert not ws._ERR_NAME_RE.match("session-20260912-101010-a.err/x")
        assert not ws._ERR_NAME_RE.match("other.err")
        assert os.sep not in "session-20260912-101010-abcd1234.err"
