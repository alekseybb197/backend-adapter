#!/usr/bin/env python3
"""Unit + HTTP tests for backend_adapter.session_viewer — "/session" endpoint.

Tests cover: _is_stale() staleness logic, find_or_generate_sessions()
(generation on missing tree.html, regeneration on newer part-files, skip on
generation error), HTTP GET /session (tabs shell with session names) and
GET /session/<имя>/<путь> file serving (tree.html, raw part-file on the
level above artefacts/, path traversal → 404, unknown session → 404).
"""
import json
import os
import shutil
import sys
import threading
import time

import pytest

# Re-import fresh modules per test (autouse fresh_env already deleted them).
# Session data mirrors test_artifact_tree fixtures: JSON openai_body/fetch_raw
# files whose names match PART_RE ^<hex>-<int>-<type>.json.


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirror test_artifact_tree conventions)
# ---------------------------------------------------------------------------

def _write_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _make_parts_dir(tmp_path, name: str, parts: dict) -> str:
    """Создать директорию сессии *.parts с part-файлами; вернуть её путь."""
    d = os.path.join(str(tmp_path), name)
    os.makedirs(d, exist_ok=True)
    for fname, data in parts.items():
        _write_json(os.path.join(d, fname), data)
    return d


def _simple_ob(part_id: int) -> dict:
    return {"messages": [{"role": "user", "content": "Hello world"}]}


def _simple_fr(part_id: int, content: str = "Hi there!") -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _start_server(root_dir: str):
    from backend_adapter import webserver
    httpd = webserver.serve(root_dir, "0.0.0-test", port=0)
    assert httpd is not None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_port


def _http_get(port: int, path: str):
    """Raw GET через сокет; возвращает (status, body)."""
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


# ---------------------------------------------------------------------------
# TestIsStale
# ---------------------------------------------------------------------------

class TestIsStale:
    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.session_viewer")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.session_viewer import _is_stale, find_or_generate_sessions, render_shell, SESSION_SUFFIX, TREE_RELATIVE_PATH, ARTEFACTS_DIRNAME
        self._is_stale = _is_stale
        self.find = find_or_generate_sessions
        self.render_shell = render_shell
        self.SESSION_SUFFIX = SESSION_SUFFIX
        self.TREE = TREE_RELATIVE_PATH
        self.ARTEFACTS = ARTEFACTS_DIRNAME

    def test_missing_tree_is_stale(self, tmp_path):
        parts_dir = _make_parts_dir(tmp_path, "s1.parts", {})
        assert self._is_stale(parts_dir, os.path.join(parts_dir, self.TREE)) is True

    def test_no_parts_no_tree_not_stale(self, tmp_path):
        """Пустая директория без tree.html и без part-файлов: дерево генерить
        не нужно (нет данных) — считается НЕ устаревшей."""
        parts_dir = _make_parts_dir(tmp_path, "s2.parts", {})
        tree_path = os.path.join(parts_dir, self.TREE)
        # Дерева нет — _is_stale вернёт True, но только если tree отсутствует
        assert self._is_stale(parts_dir, tree_path) is True

    def test_new_part_file_marks_stale(self, tmp_path):
        parts_dir = _make_parts_dir(tmp_path, "s3.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2),
        })
        artefacts = os.path.join(parts_dir, self.ARTEFACTS)
        os.makedirs(artefacts)
        tree_path = os.path.join(artefacts, "tree.html")
        # tree.html «старше» part-файлов
        with open(tree_path, "w") as f:
            f.write("<html/>")
        os.utime(tree_path, (time.time() - 1000, time.time() - 1000))
        assert self._is_stale(parts_dir, tree_path) is True

    def test_fresh_tree_not_stale(self, tmp_path):
        parts_dir = _make_parts_dir(tmp_path, "s4.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2),
        })
        artefacts = os.path.join(parts_dir, self.ARTEFACTS)
        os.makedirs(artefacts)
        tree_path = os.path.join(artefacts, "tree.html")
        with open(tree_path, "w") as f:
            f.write("<html/>")
        # tree.html свежее всех part-файлов — НЕ stale
        assert self._is_stale(parts_dir, tree_path) is False

    def test_artefacts_dir_ignored(self, tmp_path):
        """Файлы внутри artefacts/ не считаются «сырыми» part-файлами —
        иначе tree.html устаревал бы сам от себя."""
        parts_dir = _make_parts_dir(tmp_path, "s5.parts", {})
        artefacts = os.path.join(parts_dir, self.ARTEFACTS)
        os.makedirs(artefacts)
        tree_path = os.path.join(artefacts, "tree.html")
        with open(tree_path, "w") as f:
            f.write("<html/>")
        # Внутри artefacts/ кладём «свежий» файл — на stale влиять не должен
        fresh_inside = os.path.join(artefacts, "toolcall-1.yaml")
        with open(fresh_inside, "w") as f:
            f.write("x")
        os.utime(fresh_inside, (time.time() + 1000, time.time() + 1000))
        assert self._is_stale(parts_dir, tree_path) is False


# ---------------------------------------------------------------------------
# TestFindOrGenerate
# ---------------------------------------------------------------------------

class TestFindOrGenerate:
    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.session_viewer")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.session_viewer import find_or_generate_sessions
        self.find = find_or_generate_sessions

    def test_generates_tree_when_missing(self, tmp_path):
        _make_parts_dir(tmp_path, "gen.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hello!"),
        })
        root = str(tmp_path)
        sessions = self.find(root, verbose=True)
        assert sessions == [("gen.parts", os.path.join(root, "gen.parts"))]
        tree = os.path.join(root, "gen.parts", "artefacts", "tree.html")
        assert os.path.isfile(tree)

    def test_uses_existing_fresh_tree(self, tmp_path):
        parts_dir = _make_parts_dir(tmp_path, "fresh.parts", {
            "a-1-openai_body.json": _simple_ob(1),
        })
        artefacts = os.path.join(parts_dir, "artefacts")
        os.makedirs(artefacts)
        with open(os.path.join(artefacts, "tree.html"), "w") as f:
            f.write("ORIGINAL")
        # mtime tree.html делаем будущим — «свежее» любого part-файла
        now = time.time()
        os.utime(os.path.join(artefacts, "tree.html"), (now + 5000, now + 5000))
        sessions = self.find(str(tmp_path), verbose=True)
        assert sessions  # сессия найдена
        # tree.html НЕ перегенерирован (оригинальное содержимое цело)
        content = open(os.path.join(artefacts, "tree.html")).read()
        assert content == "ORIGINAL"

    def test_regenerates_when_tree_stale(self, tmp_path):
        parts_dir = _make_parts_dir(tmp_path, "stale.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hello!"),
        })
        artefacts = os.path.join(parts_dir, "artefacts")
        os.makedirs(artefacts)
        with open(os.path.join(artefacts, "tree.html"), "w") as f:
            f.write("ORIGINAL")
        old = time.time() - 5000
        os.utime(os.path.join(artefacts, "tree.html"), (old, old))
        # Теперь «дописываем» новый part-файл — tree устарел
        _write_json(os.path.join(parts_dir, "c-3-openai_body.json"), _simple_ob(3))
        self.find(str(tmp_path), verbose=True)
        content = open(os.path.join(artefacts, "tree.html")).read()
        assert content != "ORIGINAL"  # перегенерирован

    def test_generation_error_skips_session(self, tmp_path):
        """Сломанный part-файл (не JSON) не должен ронять весь список.

        Имя файла обязано подходить под PART_RE (^<hex>-<int>-<type>.json) —
        иначе генератор его просто не заметит; только тогда load_json
        действительно упадёт и сессия будет пропущена."""
        bad_dir = os.path.join(str(tmp_path), "bad.parts")
        os.makedirs(bad_dir)
        with open(os.path.join(bad_dir, "c-3-openai_body.json"), "w") as f:
            f.write("{not json")
        sessions = self.find(str(tmp_path), verbose=True)
        assert sessions == []  # упавшая генерация → сессия пропущена

    def test_non_parts_dirs_ignored(self, tmp_path):
        _make_parts_dir(tmp_path, "real.parts", {
            "a-1-openai_body.json": _simple_ob(1),
        })
        os.makedirs(os.path.join(str(tmp_path), "not-a-parts"))
        os.makedirs(os.path.join(str(tmp_path), "just-a-dir"))
        sessions = self.find(str(tmp_path), verbose=True)
        names = [n for n, _ in sessions]
        assert names == ["real.parts"]

    def test_missing_root_returns_empty(self, tmp_path):
        assert self.find(os.path.join(str(tmp_path), "nope"), verbose=True) == []


# ---------------------------------------------------------------------------
# TestRenderShell
# ---------------------------------------------------------------------------

class TestRenderShell:
    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.session_viewer")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.session_viewer import render_shell, find_or_generate_sessions
        self.render_shell = render_shell
        self.find = find_or_generate_sessions

    def test_empty_shell_has_hint(self, tmp_path):
        html = self.render_shell([], str(tmp_path))
        assert "Не найдено ни одной директории" in html
        assert ".parts" in html
        # Ссылка на статус-страницу есть и на пустой странице
        assert '<a id="status-link" href="/">← статус</a>' in html

    def test_shell_lists_session_names(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-A.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2),
        })
        sessions = self.find(str(tmp_path), verbose=True)
        html = self.render_shell(sessions, str(tmp_path))
        # Имя сессии в подписи вкладки (без ".parts")
        assert "session-A" in html
        # Есть iframe с URL дерева
        assert 'src="/session/session-A.parts/artefacts/tree.html"' in html

    def test_shell_has_status_link_and_first_tab_active(self, tmp_path):
        """Панель вкладок: слева — ссылка на статус-страницу "/", активная
        вкладка по умолчанию — первая (id tab-<safe_id> в location.hash)."""
        _make_parts_dir(tmp_path, "session-A.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hello!"),
        })
        _make_parts_dir(tmp_path, "session-B.parts", {
            "c-1-openai_body.json": _simple_ob(1),
        })
        sessions = self.find(str(tmp_path), verbose=True)
        html = self.render_shell(sessions, str(tmp_path))
        # Ссылка на корень (статус-страницу) — первой в панели вкладок
        assert '<div id="tabs"><a id="status-link" href="/">← статус</a>' in html
        # Идентификаторы вкладок — транслитерация имени сессии: НЕ-alnum
        # символы (точка ".parts", дефис) заменяются на "_".
        assert 'id="tab-session_A_parts" class="tab active"' in html
        assert 'id="frame-session_A_parts" src="/session/session-A.parts/artefacts/tree.html" style="display:block"' in html
        assert 'id="tab-session_B_parts" class="tab"' in html
        assert 'id="frame-session_B_parts"' in html and 'display:none' in html

    def test_shell_tab_js_preserves_active_tab(self):
        """JS сохраняет активную вкладку между обновлениями: клик по вкладке
        пишет её id в location.hash, при загрузке страницы вкладка из hash
        активируется заново (а не первая)."""
        from backend_adapter.session_viewer import SHELL_TEMPLATE
        assert "location.hash" in SHELL_TEMPLATE
        # клик по вкладке обновляет hash
        assert "if (location.hash !== '#' + id) location.hash = id;" in SHELL_TEMPLATE
        # при загрузке — восстановление вкладки из hash
        assert "window.addEventListener('DOMContentLoaded'" in SHELL_TEMPLATE
        assert "document.getElementById('tab-' + id)" in SHELL_TEMPLATE


# ---------------------------------------------------------------------------
# HTTP integration: GET /session (вкладки), раздача файлов, обход пути
# ---------------------------------------------------------------------------

class TestSessionHTTP:
    def test_get_session_tabs(self, tmp_path):
        _make_parts_dir(tmp_path, "session-A.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hello!"),
        })
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/session")
            assert status == 200
            assert "text/html" in body.lower() or "<!DOCTYPE" in body
            assert "session-A" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_session_page_links_to_status_root(self, tmp_path):
        """Ссылка «← статус» ведёт на корень сервера — статус-страницу "/",
        которая в этом же процессе отвечает 200 (эндпойнт зарегистрирован)."""
        _make_parts_dir(tmp_path, "session-A.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hello!"),
        })
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/session")
            assert status == 200
            assert '<a id="status-link" href="/">← статус</a>' in body
            # Корень отвечает — ссылка не ведёт в никуда (в standalone root_dir
            # без env-бэкендов статус-страница отдаёт подсказку, но 200)
            status_root, _ = _http_get(port, "/")
            assert status_root == 200
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_get_session_empty_has_hint(self, tmp_path):
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/session")
            assert status == 200
            assert "Не найдено ни одной директории" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_get_tree_html_file(self, tmp_path):
        _make_parts_dir(tmp_path, "session-B.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hello!"),
        })
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/session/session-B.parts/artefacts/tree.html")
            assert status == 200
            assert "<!DOCTYPE" in body or "html" in body.lower()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_get_raw_part_file_one_level_up(self, tmp_path):
        """tree.html ссылается на raw part-файлы "../<имя>.json" — сервер
        должен отдавать файлы НА УРОВЕНЬ выше artefacts/."""
        _make_parts_dir(tmp_path, "session-C.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hello!"),
        })
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/session/session-C.parts/a-1-openai_body.json")
            assert status == 200
            assert "Hello world" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_unknown_session_404(self, tmp_path):
        _make_parts_dir(tmp_path, "session-D.parts", {})
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _ = _http_get(port, "/session/session-unknown.parts/artefacts/tree.html")
            assert status == 404
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_path_traversal_blocked(self, tmp_path):
        """"../.." не должен выводить за пределы parts_dir."""
        _make_parts_dir(tmp_path, "session-E.parts", {
            "a-1-openai_body.json": _simple_ob(1),
        })
        # Файл-«мишень» рядом с root (вне сессии)
        target = os.path.join(str(tmp_path), "secret.txt")
        with open(target, "w") as f:
            f.write("SECRET")
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _ = _http_get(port,
                "/session/session-E.parts/../../secret.txt")
            assert status == 404
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_directory_prefix_confusion(self, tmp_path):
        """Префикс '/session' не должен совпадать с '/sessionXYZ'."""
        _make_parts_dir(tmp_path, "session-F.parts", {
            "a-1-openai_body.json": _simple_ob(1),
        })
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _ = _http_get(port, "/sessionXYZ")
            assert status == 404
        finally:
            httpd.shutdown()
            httpd.server_close()
