#!/usr/bin/env python3
"""Unit + integration tests for backend_adapter.webserver — shared WEBUI core.

Tests cover: endpoint registry/register(), Handler prefix routing (exact "/",
nested "/session/...", longest-prefix, unknown path → 404), serve()
(non-directory root → None; port 0 → listens; context version/root_dir
reaches endpoints; 405 for a method the endpoint did not implement).
"""
import json
import os
import re
import sys
import threading
import time

import pytest

# Re-import modules fresh each test (fresh_env autouse fixture deleted them).
# The endpoint modules import webserver at module level, so a fresh webserver
# import also resets the ENDPOINTS registry (empty until modules imported).


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _start_server(root_dir: str, version: str = "0.0.0-test", port: int = 0):
    """Start WEBUI server in a daemon thread on an ephemeral port (0).

    Returns (httpd, actual_port). Caller must httpd.shutdown()/server_close().
    serve() imports the endpoint modules (session_viewer/webui_status), which
    registers them into webserver.ENDPOINTS — so a fresh import per test is
    what the integration tests actually exercise."""
    from backend_adapter import webserver
    httpd = webserver.serve(root_dir, version, port=port)
    assert httpd is not None, "serve() вернул None для валидной директории"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_port


def _http_get(port: int, path: str):
    """Raw GET через сокет (HTTP/1.0) — конвенция тестов test_server.py."""
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
        return {"status": status, "body": body}
    finally:
        sock.close()


def _http_post(port: int, path: str):
    """Raw POST через сокет (HTTP/1.0) с пустым телом."""
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
        return {"status": status, "body": body}
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# TestEndpoint (local dummy for routing tests)
# ---------------------------------------------------------------------------

class _DummyEndpoint:
    """Минимальный двойник webserver.Endpoint (без импорта цикла)."""

    prefix = "/dummy"

    def __init__(self, context):
        self.context = context


# ---------------------------------------------------------------------------
# TestEndpointBase
# ---------------------------------------------------------------------------

class TestEndpointBase:
    def test_endpoint_default_get_404(self):
        # Реальный базовый класс: GET без реализации → 404.
        from backend_adapter import webserver
        ep = webserver.Endpoint()
        assert ep.prefix == ""
        # Методы по умолчанию существуют и отвечают 404/405 (через handler)
        class FakeHandler:
            def send_error(self, code, msg):
                self._code = code
        h = FakeHandler()
        ep.GET(h, "")
        assert h._code == 404
        ep.POST(h, "")
        assert h._code == 405


# ---------------------------------------------------------------------------
# TestRegister
# ---------------------------------------------------------------------------

class TestRegister:
    def test_register_adds_to_registry(self):
        from backend_adapter import webserver
        # Очищаем реестр для теста
        webserver.ENDPOINTS.clear()
        cls = webserver.register(_DummyEndpoint)
        assert cls is _DummyEndpoint  # декоратор возвращает класс как есть
        assert _DummyEndpoint in webserver.ENDPOINTS

    def test_register_dedup(self):
        from backend_adapter import webserver
        webserver.ENDPOINTS.clear()
        webserver.register(_DummyEndpoint)
        webserver.register(_DummyEndpoint)  # повторная регистрация — no-op
        assert webserver.ENDPOINTS.count(_DummyEndpoint) == 1


# ---------------------------------------------------------------------------
# TestHandlerMatch
# ---------------------------------------------------------------------------

class TestHandlerMatch:
    def setup_method(self):
        # Свежий webserver и пара фейковых эндпойнтов для _match
        from backend_adapter import webserver
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.webserver")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter import webserver as w
        self.ws = w

        class RootEp(w.Endpoint):
            prefix = "/"
        class SessionEp(w.Endpoint):
            prefix = "/session"
        class SessionFilesEp(w.Endpoint):
            prefix = "/session/deep"

        # Базовая Endpoint не объявляет __init__ (context нужен только
        # конкретным эндпойнтам) — для _match важен лишь атрибут класса
        # prefix, поэтому инстансируем без аргументов.
        self.root = RootEp()
        self.session = SessionEp()
        self.session_files = SessionFilesEp()

    def _match(self, path):
        # Handler._match читает только self.endpoints — подкладываем
        # инстанс-пустышку со списком фейковых эндпойнтов.
        h = object.__new__(self.ws.Handler)
        h.endpoints = [self.root, self.session, self.session_files]
        return self.ws.Handler._match(h, path)

    def test_exact_root(self):
        ep, rem = self._match("/")
        assert ep is self.root
        assert rem == ""

    def test_root_does_not_capture_nested(self):
        # prefix "/" совпадает ТОЛЬКО с точным путём "/" — "/session" уходит
        # эндпойнту сессий, а не корневому.
        ep, rem = self._match("/session")
        assert ep is self.session
        assert rem == ""

    def test_nested_session_remainder(self):
        ep, rem = self._match("/session/session-1.parts/artefacts/tree.html")
        assert ep is self.session
        assert rem == "session-1.parts/artefacts/tree.html"

    def test_longest_prefix_wins(self):
        # Оба эндпойнта подходят; побеждает самый длинный префикс
        ep, rem = self._match("/session/deep/file.yaml")
        assert ep is self.session_files
        assert rem == "file.yaml"

    def test_prefix_requires_boundary(self):
        # "/sessionX" не должен матчить "/session" (нужен слэш или конец)
        ep, _ = self._match("/sessionX")
        assert ep is None

    def test_unknown_path_404(self):
        ep, _ = self._match("/nonexistent")
        assert ep is None


# ---------------------------------------------------------------------------
# TestServe
# ---------------------------------------------------------------------------

class TestServe:
    def test_nonexistent_root_returns_none(self):
        from backend_adapter import webserver
        assert webserver.serve("/definitely/not/a/dir", "0.0.0-test") is None

    def test_file_root_returns_none(self, tmp_path):
        from backend_adapter import webserver
        f = tmp_path / "afile.txt"
        f.write_text("x")
        assert webserver.serve(str(f), "0.0.0-test") is None

    def test_context_reaches_endpoints(self, tmp_path):
        # version и root_dir из serve() должны дойти до инстансов эндпойнтов
        root = str(tmp_path / "logs")
        os.makedirs(root)
        from backend_adapter import webserver
        # Пустой реестр — встроенные эндпойнты пока не импортированы.
        # Проверяем через register своего наблюдателя ДО serve() (serve
        # инстанцирует всё, что уже в реестре + импортирует встроенные).
        seen = {}

        class ProbeEp(webserver.Endpoint):
            prefix = "/probe"
            def __init__(self, context):
                seen["context"] = context
            def GET(self, handler, remainder):
                pass

        webserver.register(ProbeEp)
        httpd = webserver.serve(root, "9.9.9-test", port=0)
        assert httpd is not None
        try:
            assert seen["context"].version == "9.9.9-test"
            assert seen["context"].root_dir == os.path.abspath(root)
            assert seen["context"].verbose is False
        finally:
            httpd.server_close()

    def test_serve_integration_unknown_path_404(self, tmp_path):
        root = str(tmp_path / "logs")
        os.makedirs(root)
        httpd, port = _start_server(root)
        try:
            r = _http_get(port, "/no/such/endpoint")
            assert r["status"] == 404
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_serve_integration_405_on_unimplemented_method(self, tmp_path):
        # Статус-эндпойнт "/" реализует GET и POST. Эндпойнт сессий —
        # только GET: POST на /session должен дать 405.
        root = str(tmp_path / "logs")
        os.makedirs(root)
        httpd, port = _start_server(root)
        try:
            r = _http_post(port, "/session")
            assert r["status"] in (405,)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_serve_returns_threading_server(self, tmp_path):
        root = str(tmp_path / "logs")
        os.makedirs(root)
        from backend_adapter import webserver
        httpd = webserver.serve(root, "0.0.0-test", port=0)
        try:
            import http.server
            assert isinstance(httpd, http.server.ThreadingHTTPServer)
            assert httpd.daemon_threads is True
        finally:
            httpd.server_close()

    def test_cli_standalone_serves_builtin_endpoints(self, tmp_path):
        """python -m регрессия (CLI-запуск ядра): runpy исполняет модуль как
        __main__ ДВАЖДЫ — каноническая копия в sys.modules (в неё эндпойнт-
        модули регистрируются через `from . import ...`) и отдельный namespace
        __main__ с ПУСТЫМ реестром ENDPOINTS. Если бы serve() звался из
        namespace __main__, все эндпойнты остались бы незарегистрированными
        и сервер отвечал бы 404 на каждый путь (баг воспроизведён вручную,
        починен трамплином `from backend_adapter.webserver import main` в
        `if __name__ == "__main__"`).

        Поведенческая проверка: настоящий `python -m backend_adapter.webserver`
        в субпроцессе на порту 0; встроенные эндпойнты ("/" и "/session")
        обязаны отвечать не-404."""
        import socket
        import subprocess
        root = str(tmp_path / "logs")
        os.makedirs(root)
        # Свободный порт: занимаем сокет, запоминаем номер, отпускаем — сервер
        # субпроцесса успеет его занять (тонкая гонка; 8799 мог быть занят).
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-m", "backend_adapter.webserver", root,
             "--port", str(port)],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 10
            statuses = {}
            while time.time() < deadline:
                for path in ("/", "/session"):
                    try:
                        r = _http_get(port, path)
                        statuses[path] = r["status"]
                    except Exception:
                        pass
                if len(statuses) == 2 and statuses.get("/") not in (None,):
                    break
                time.sleep(0.2)
            assert statuses.get("/") == 200, f"GET / → {statuses.get('/')}"
            assert statuses.get("/session") == 200, \
                f"GET /session → {statuses.get('/session')}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_serve_registers_builtin_endpoints(self, tmp_path):
        # serve() импортирует session_viewer/webui_status — их классы
        # должны оказаться в реестре (и, значит, обслуживаться).
        root = str(tmp_path / "logs")
        os.makedirs(root)
        from backend_adapter import webserver
        webserver.ENDPOINTS.clear()
        httpd = webserver.serve(root, "0.0.0-test", port=0)
        assert httpd is not None
        try:
            from backend_adapter import session_viewer, webui_status
            assert webserver.Handler.endpoints  # инстансы созданы
            prefixes = {ep.prefix for ep in webserver.Handler.endpoints}
            assert "/" in prefixes
            assert "/session" in prefixes
        finally:
            httpd.server_close()

    def test_empty_root_serves_status_page(self, tmp_path):
        """Zero-config WEBUI-корень (./tmp/webui при пустом ADAPTER_DEBUG_LOGPATH):
        пустая, заранее созданная папка — валидный корень; статус-страница "/"
        отвечает 200, /session — тоже 200 (пустая страница, .parts нет)."""
        root = str(tmp_path / "webui")
        os.makedirs(root)
        httpd, port = _start_server(root)
        try:
            r = _http_get(port, "/")
            assert r["status"] == 200
            r_session = _http_get(port, "/session")
            assert r_session["status"] == 200
        finally:
            httpd.shutdown()
            httpd.server_close()
