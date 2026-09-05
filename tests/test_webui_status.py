#!/usr/bin/env python3
"""Unit + HTTP tests for backend_adapter.webui_status — status page at "/".

Tests cover: _config_snapshot() mode detection (multi-backend / standalone),
_collect_endpoints() grouping, the «Доступные API» column (rendered from
config._ENDPOINT_STATE — result of the smoke probe run inside
refresh_models; a green ✓ is shown only for endpoints that answered HTTP
200 — failed probes are hidden), the background-check contract: GET "/"
renders the current state from config.refresh_state() (seeded as
_REFRESH_JOB here); POST "/" (the «⟳ Проверить сейчас» button) launches
config.start_refresh(timeout=PROBE_TIMEOUT) and answers 303 See Other →
GET "/" (PRG pattern: the page is shown via a plain GET, so reloads never
repeat the POST and no «resubmit» dialog appears); while a check is running
the page shows the «Проверка выполняется…» banner plus the status_poll JS
(polls /api/refresh-state, reloads on completion). Success shows the new
model list, failure keeps the old cache and shows the error text, standalone
(no endpoints) renders the notice and does not start any check. The Models
cell (_models_html) is capped at MODEL_LINES rows with an expand/collapse
button (JS models_toggle on the page) — see TestModelsCell.
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


def _http_raw(port: int, method: str, path: str):
    """Сырой ответ: (status, headers: dict, body) — для 303/Location-проверок.

    _http_request возвращает только статус и тело; здесь нужны и заголовки
    (Location редиректа POST "/"), поэтому парсим полный ответ сами."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        body = ""
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


def _fresh_modules():
    """Переимпорт свежих модулей (после autouse fresh_env) в экземплярах."""
    from backend_adapter import config
    from backend_adapter import webui_status
    return config, webui_status


def _done_job(ok, count, errors=None, checked_at="12:00:00") -> dict:
    """Снимок завершённой проверки (как публикует config._refresh_worker)."""
    return {
        "running": False,
        "started_at": 900.0,
        "done_at": 901.0,
        "ok": ok,
        "count": count,
        "errors": errors or {},
        "checked_at": checked_at,
    }


def _running_job() -> dict:
    """Снимок ИДУЩЕЙ проверки (результата ещё нет — ok/count/errors None)."""
    return {
        "running": True,
        "started_at": 1000.0,
        "done_at": None,
        "ok": None,
        "count": None,
        "errors": None,
        "checked_at": None,
    }


def _seed_job(config, job: dict):
    """Опубликовать снимок состояния (config.refresh_state() вернёт его копию)."""
    config._REFRESH_JOB = job


def _seed_endpoint_state(config, backend_name: str, paths: dict):
    """Заполнить config._ENDPOINT_STATE результатом пробы (пути без /v1/).

    ``paths`` — {короткий_путь: {"found": bool, "status": int|None}}; путь
    отдаётся как есть (рендер сравнивает по коротким именам)."""
    config._ENDPOINT_STATE[backend_name] = {
        "at": 1.0,
        "endpoints": {f"/v1/{p}": v for p, v in paths.items()},
        "errors": {},
    }


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
# Колонка «Доступные API»: из config._ENDPOINT_STATE (результат пробы)
# ---------------------------------------------------------------------------

class TestApiColumn:
    def _ctx(self):
        return mock.Mock(version="0.0.0-test")

    def _seed(self, config, ws, found=None, api_state=None, refresh=None):
        """Общий посев: бэкенд AAA, модели, проба-состояние и refresh."""
        backend = {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"}
        config._BACKENDS = [backend]
        config._MODEL_TO_BACKEND = {"m-a": ("AAA", backend)}
        if api_state is not None:
            config._ENDPOINT_STATE["AAA"] = api_state
        if refresh is None:
            refresh = _done_job(True, 1)
        return ws._render_status_page(self._ctx(), refresh=refresh).decode()

    def test_collect_endpoints_reads_probe_state(self):
        # api берётся из config._ENDPOINT_STATE и нормализуется (пути без
        # "/v1/"-префикса, порядок значений неважен — рендер восстановит).
        config, ws = _fresh_modules()
        backend = {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"}
        config._BACKENDS = [backend]
        config._MODEL_TO_BACKEND = {"m-a": ("AAA", backend)}
        config._ENDPOINT_STATE["AAA"] = {
            "at": 1.0,
            "endpoints": {
                "/v1/chat/completions": {"found": True, "status": 200},
                "/v1/messages": {"found": False, "status": 404},
            },
            "errors": {},
        }
        ep = ws._collect_endpoints()[0]
        assert ep["api"] == {
            "chat/completions": {"found": True, "status": 200},
            "messages": {"found": False, "status": 404},
        }

    def test_collect_endpoints_no_state_means_none(self):
        # Бэкенд без записи в _ENDPOINT_STATE — api None (не пробован:
        # standalone / проба выключена).
        config, ws = _fresh_modules()
        backend = {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"}
        config._BACKENDS = [backend]
        config._MODEL_TO_BACKEND = {"m-a": ("AAA", backend)}
        assert ws._collect_endpoints()[0]["api"] is None

    def test_render_shows_found_and_hides_not_found(self):
        # Работающий путь (200) — зелёным с ✓; непрошедший (404) не показывается.
        config, ws = _fresh_modules()
        _seed_endpoint_state(config, "AAA", {
            "chat/completions": {"found": True, "status": 200},
            "messages": {"found": False, "status": 404},
        })
        body = self._seed(config, ws)
        assert "chat/completions ✓" in body
        assert "messages" not in body

    def test_render_status_400_hidden(self):
        # 400 — эндпоинт не прошёл проверку (found=False): на странице его нет.
        config, ws = _fresh_modules()
        _seed_endpoint_state(config, "AAA", {
            "messages": {"found": False, "status": 400},
        })
        body = self._seed(config, ws)
        assert "messages" not in body
        assert "✓" not in body

    def test_render_unprobed_shows_not_probed(self):
        # Бэкенд вообще не пробован (нет записи в _ENDPOINT_STATE) — «не опрошено».
        config, ws = _fresh_modules()
        body = self._seed(config, ws)
        assert "не опрошено" in body

    def test_render_missing_endpoint_hidden(self):
        # Пропущенный эндпоинт (probe-модель не найдена → в результат не попал):
        # в ячейке не показывается; виден только реально работающий (200).
        config, ws = _fresh_modules()
        _seed_endpoint_state(config, "AAA", {
            "chat/completions": {"found": True, "status": 200},
        })
        body = self._seed(config, ws)
        assert "chat/completions ✓" in body
        assert "messages" not in body
        assert "responses" not in body
        assert "embeddings" not in body

    def test_render_all_unavailable_shows_placeholder(self):
        # Проба была, но ни один эндпоинт не ответил 200 — серая «—» вместо
        # пустой ячейки (никаких зелёных ✓).
        config, ws = _fresh_modules()
        _seed_endpoint_state(config, "AAA", {
            "chat/completions": {"found": False, "status": 404},
            "messages": {"found": False, "status": 501},
            "responses": {"found": False, "status": 400},
            "embeddings": {"found": False, "status": None},
        })
        body = self._seed(config, ws)
        assert "ни один эндпоинт не ответил HTTP 200" in body
        assert "✓" not in body

    def test_header_and_api_cell_present(self):
        config, ws = _fresh_modules()
        body = self._seed(config, ws)
        # Заголовки колонок — английские: Backend/Base URL/Status/Endpoints/Models
        assert "Backend" in body and "Endpoints" in body and "Models" in body
        assert "<th>Backend</th>" in body
        assert "<th>Base URL</th>" in body
        assert "<th>Status</th>" in body
        assert "<th>Endpoints</th>" in body
        assert "<th>Models</th>" in body

    def test_standalone_renders_unprobed_without_refresh(self):
        # Standalone: refresh не делался — ячейки «не опрошено», никакой сети.
        config, ws = _fresh_modules()
        config.ADAPTER_BACKEND_CONFIG = ""
        body = ws._render_status_page(self._ctx()).decode()
        assert "нет данных" in body  # нет строк вообще

    def test_render_page_footer_mentions_api_check(self):
        # refresh is None (standalone без эндпойнтов) — футер про авто-проверку.
        config, ws = _fresh_modules()
        config.ADAPTER_BACKEND_CONFIG = ""
        body = ws._render_status_page(self._ctx()).decode()
        assert "API-эндпойнтов" in body or "max_tokens" in body


# ---------------------------------------------------------------------------
# TestModelsCell — список моделей: 4 строки + кнопка «Показать ещё (N)»
# ---------------------------------------------------------------------------

class TestModelsCell:
    def test_few_models_no_button(self):
        # ≤ MODEL_LINES моделей — все видны, кнопки/span-хвоста нет.
        config, ws = _fresh_modules()
        models = [f"m{i}" for i in range(ws.MODEL_LINES)]
        html = ws._models_html(models, "ok")
        assert html.count('<div style="line-height:1.5">') == ws.MODEL_LINES
        assert "models-extra" not in html
        assert "Показать ещё" not in html

    def test_many_models_limited_with_button(self):
        # > MODEL_LINES моделей — видны первые MODEL_LINES, остальные в
        # скрытом span, кнопка «Показать ещё (N)» с числом скрытых строк.
        config, ws = _fresh_modules()
        models = [f"m{i}" for i in range(7)]
        html = ws._models_html(models, "ok")
        # первые MODEL_LINES строк — видимые div'ы (вне скрытого span)
        visible = html.split('<span class="models-extra"')[0]
        assert visible.count('<div style="line-height:1.5">') == ws.MODEL_LINES
        assert '<span class="models-extra" style="display:none">' in html
        # в скрытом span — остальные (7 - MODEL_LINES) строк
        extra = html.split('<span class="models-extra" style="display:none">')[1]
        extra = extra.split("</span>")[0]
        assert extra.count('<div style="line-height:1.5">') == 7 - ws.MODEL_LINES
        assert f"Показать ещё ({7 - ws.MODEL_LINES})" in html
        # кнопка вызывает models_toggle и несёт общее число моделей
        assert 'onclick="models_toggle(this)"' in html
        assert 'data-models-count="7"' in html

    def test_empty_shows_status_text(self):
        # Нет моделей — строка-заглушка со статусом (для колонки Models).
        config, ws = _fresh_modules()
        assert ws._models_html([], "недоступен") == (
            '<span style="color:#999">недоступен</span>'
        )

    def test_page_contains_toggle_script(self):
        # Полная страница несёт JS models_toggle (работа кнопки «Свернуть»),
        # а модели рендерятся как отдельные строки (не запятыми).
        config, ws = _fresh_modules()
        backend = {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"}
        config._BACKENDS = [backend]
        config._MODEL_TO_BACKEND = {f"m{i}": ("AAA", backend) for i in range(6)}
        ctx = mock.Mock(version="0.0.0-test")
        body = ws._render_status_page(ctx, refresh=_done_job(True, 6)).decode()
        assert "function models_toggle(btn)" in body
        assert "Свернуть" in body  # JS меняет текст кнопки на «Свернуть»
        # первая модель видна строкой, а её хвост свёрнут в models-extra
        assert "m0" in body and '<span class="models-extra" style="display:none">' in body


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
        body = ws._render_status_page(self._ctx(), refresh=_done_job(True, 1)).decode()
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
        refresh = _done_job(True, 2, errors={"BBB": "Connection refused by test"})
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
        refresh = _done_job(False, 1, errors={"AAA": "Connection refused by test"})
        body = ws._render_status_page(self._ctx(), refresh=refresh).decode()
        assert "old-m" in body            # старый кэш не стёрт
        assert "Не удалось обновить" in body
        assert "Connection refused by test" in body

    def test_refresh_none_footer_mentions_button(self):
        config, ws = _fresh_modules()
        body = ws._render_status_page(self._ctx()).decode()
        assert "по кнопке" in body and "Проверить сейчас" in body

    def test_endpoint_absent_from_errors_after_partial_is_ok(self):
        # Бэкенд без ошибки в refresh — статус из snapshot («ok»), даже если
        # другой бэкенд упал.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
            {"name": "BBB", "base": "http://bbb.local", "key": "k-bbb"},
        ]
        config._MODEL_TO_BACKEND = {"m-a": ("AAA", config._BACKENDS[0])}
        refresh = _done_job(True, 1, errors={"BBB": "boom"})
        body = ws._render_status_page(self._ctx(), refresh=refresh).decode()
        assert '<span style="color:#1a7f37">ok</span>' in body


# ---------------------------------------------------------------------------
# HTTP: GET "/" читает состояние, POST "/" — PRG: 303 на GET "/"
# ---------------------------------------------------------------------------

def _start_httpd(tmp_path):
    httpd, port = _start_server(str(tmp_path))
    return httpd, port


class TestStatusHTTP:
    def test_get_root_renders_version_and_state(self, tmp_path):
        # GET "/" НЕ запускает проверку: показывает состояние последней
        # (посев _REFRESH_JOB). start_refresh/refresh_models не вызываются.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}
        _seed_job(config, _done_job(True, 1, checked_at="12:34:56"))

        with mock.patch.object(config, "start_refresh", return_value=False) as m_start:
            with mock.patch.object(config, "refresh_models", return_value={}) as m_refresh:
                httpd, port = _start_server(str(tmp_path))
                try:
                    status, body = _http_get(port, "/")
                    assert status == 200
                    assert "0.0.0-test" in body       # версия из контекста сервера
                    assert "AAA" in body
                    assert "http://aaa.local" in body
                    assert "Список моделей обновлён" in body
                    assert "12:34:56" in body
                finally:
                    httpd.shutdown()
                    httpd.server_close()
        assert m_start.call_count == 0   # GET проверку не запускает
        assert m_refresh.call_count == 0

    def test_get_root_initial_state_no_check_hint(self, tmp_path):
        # Проверок ещё не было (_REFRESH_JOB None): страница показывает
        # snapshot и футер-подсказку «по кнопке», проверку не запускает.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}
        with mock.patch.object(config, "start_refresh", return_value=False) as m_start:
            httpd, port = _start_server(str(tmp_path))
            try:
                status, body = _http_get(port, "/")
                assert status == 200
                assert "по кнопке" in body
                assert "Проверить сейчас" in body
            finally:
                httpd.shutdown()
                httpd.server_close()
        assert m_start.call_count == 0

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
        _seed_job(config, _done_job(True, 2))
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
        # Провал проверки не роняет страницу: состояние ошибки из снимка.
        config, _ = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"old-m": ("AAA", config._BACKENDS[0])}
        _seed_job(config, _done_job(False, 1, errors={"AAA": "boom"}))
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/")
            assert status == 200
            assert "old-m" in body
            assert "Не удалось обновить" in body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_get_root_standalone_notice_no_check(self, tmp_path):
        # Standalone без конфига: эндпойнтов нет — start_refresh не
        # вызывается, страница показывает подсказку (не example.com).
        config, _ = _fresh_modules()
        config.ADAPTER_BACKEND_CONFIG = ""
        with mock.patch.object(config, "start_refresh", return_value=False) as m_start:
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
        assert m_start.call_count == 0

    def test_get_root_shows_api_column_from_state(self, tmp_path):
        # Полный путь: GET "/" рендерит колонку «Доступные API» из
        # config._ENDPOINT_STATE (в проде туда пишет probe_endpoints,
        # выполняющийся внутри refresh_models фоновой проверки).
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}
        _seed_endpoint_state(config, "AAA", {
            "chat/completions": {"found": True, "status": 200},
            "messages": {"found": False, "status": 404},
        })
        _seed_job(config, _done_job(True, 1))
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/")
            assert status == 200
            assert "<th>Endpoints</th>" in body  # заголовок колонки API
            assert "chat/completions ✓" in body
            assert "messages" not in body  # 404 — на странице не показывается
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_starts_background_check_and_redirects(self, tmp_path):
        # POST "/" (кнопка): PRG — вызывает start_refresh(timeout=
        # PROBE_TIMEOUT), отвечает 303 See Other с Location "/"; следующий
        # GET (куда уводит браузер) — обычная загрузка страницы, POST не
        # повторяется. side_effect публикует running-снимок, как настоящий
        # start_refresh: GET видит идущую проверку и вторую не запускает.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}

        def _start_running(timeout: float = 5.0):
            config._REFRESH_JOB = _running_job()
            return True

        with mock.patch.object(config, "start_refresh", side_effect=_start_running) as m_start:
            httpd, port = _start_server(str(tmp_path))
            try:
                status, headers, body = _http_raw(port, "POST", "/")
                assert status == 303
                assert headers.get("location") == "/"
                assert body == ""
                # GET после 303 — страница с баннером (проверка «идёт»)
                status2, body2 = _http_get(port, "/")
                assert status2 == 200
                assert "Проверка выполняется" in body2
            finally:
                httpd.shutdown()
                httpd.server_close()
        assert m_start.call_count == 1  # GET вторую проверку не запустил
        assert m_start.call_args.kwargs.get("timeout") == ws.PROBE_TIMEOUT

    def test_post_when_check_running_redirects_then_banner(self, tmp_path):
        # Проверка уже идёт (running=True в состоянии): POST — 303; GET после
        # редиректа рендерит баннер «Проверка выполняется» (start_refresh не
        # запускает второй поток — вернул бы False).
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}
        _seed_job(config, _running_job())
        with mock.patch.object(config, "start_refresh", return_value=False):
            httpd, port = _start_server(str(tmp_path))
            try:
                status, headers, body = _http_raw(port, "POST", "/")
                assert status == 303
                assert headers.get("location") == "/"
                assert body == ""
                status2, body2 = _http_get(port, "/")
                assert status2 == 200
                assert "Проверка выполняется" in body2
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_post_refresh_failure_redirects_then_error_page(self, tmp_path):
        # Провал фоновой проверки (состояние ok=False) не роняет POST:
        # 303 → GET показывает текст ошибки.
        config, _ = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}
        _seed_job(config, _done_job(False, 1, errors={"AAA": "Connection refused by unit test"}))
        with mock.patch.object(config, "start_refresh", return_value=False):
            httpd, port = _start_server(str(tmp_path))
            try:
                status, headers, body = _http_raw(port, "POST", "/")
                assert status == 303
                assert headers.get("location") == "/"
                status2, body2 = _http_get(port, "/")
                assert status2 == 200
                assert "недоступен" in body2
                assert "Connection refused" in body2
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_post_standalone_redirects_without_check(self, tmp_path):
        # Standalone без конфига: эндпойнтов нет — кнопка проверку не
        # запускает (нечего проверять); POST — 303, GET показывает подсказку.
        config, _ = _fresh_modules()
        config.ADAPTER_BACKEND_CONFIG = ""
        with mock.patch.object(config, "start_refresh", return_value=False) as m_start:
            httpd, port = _start_server(str(tmp_path))
            try:
                status, headers, body = _http_raw(port, "POST", "/")
                assert status == 303
                assert headers.get("location") == "/"
                status2, body2 = _http_get(port, "/")
                assert status2 == 200
                assert "Данные адаптера недоступны" in body2
            finally:
                httpd.shutdown()
                httpd.server_close()
        assert m_start.call_count == 0


# ---------------------------------------------------------------------------
# Баннер и авто-обновление: running=True → баннер + JS status_poll
# ---------------------------------------------------------------------------

class TestBannerPolling:
    def _ctx(self):
        return mock.Mock(version="0.0.0-test")

    def test_running_page_has_banner_and_poll_script(self):
        # Идёт проверка → страница несёт баннер «Проверка выполняется…»,
        # время старта из started_at и JS status_poll → /api/refresh-state.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}
        running = _running_job()
        body = ws._render_status_page(
            self._ctx(),
            refresh=None,
            running=running["running"],
            started_at=running["started_at"],
        ).decode()
        assert "Проверка выполняется" in body
        assert "запущена в" in body
        assert 'fetch("/api/refresh-state")' in body
        assert "status_poll" in body
        assert "location.reload()" in body

    def test_idle_page_has_no_poll_script(self):
        # Проверка не идёт → баннера и поллинга НЕТ (страница не будет
        # перезагружаться сама по себе).
        config, ws = _fresh_modules()
        body = ws._render_status_page(self._ctx(), refresh=None).decode()
        assert "Проверка выполняется" not in body
        assert "status_poll" not in body
        assert "fetch(\"/api/refresh-state\")" not in body

    def test_get_running_does_not_start_check(self, tmp_path):
        # GET "/", пока проверка идёт: баннер + поллинг; start_refresh не
        # вызывается (авто-релоад после завершения безопасен).
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}
        _seed_job(config, _running_job())
        with mock.patch.object(config, "start_refresh", return_value=False) as m_start:
            httpd, port = _start_server(str(tmp_path))
            try:
                status, body = _http_get(port, "/")
                assert status == 200
                assert "Проверка выполняется" in body
                assert "status_poll" in body
            finally:
                httpd.shutdown()
                httpd.server_close()
        assert m_start.call_count == 0


# ---------------------------------------------------------------------------
# /api/refresh-state: JSON-состояние проверки для JS поллинга
# ---------------------------------------------------------------------------

class TestRefreshStateAPI:
    def test_returns_running_json(self, tmp_path):
        config, ws = _fresh_modules()
        _seed_job(config, _running_job())
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/api/refresh-state")
            assert status == 200
            import json
            state = json.loads(body)
            assert state["running"] is True
            assert state["ok"] is None
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_returns_done_json(self, tmp_path):
        config, ws = _fresh_modules()
        _seed_job(config, _done_job(True, 3, checked_at="09:08:07"))
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/api/refresh-state")
            assert status == 200
            import json
            state = json.loads(body)
            assert state["running"] is False
            assert state["ok"] is True
            assert state["count"] == 3
            assert state["checked_at"] == "09:08:07"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_initial_state_defaults_json(self, tmp_path):
        # Проверок ещё не было — эндпоинт отдаёт дефолт (running=False...).
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, body = _http_get(port, "/api/refresh-state")
            assert status == 200
            import json
            state = json.loads(body)
            assert state["running"] is False
            assert state["ok"] is None
            assert state["checked_at"] is None
        finally:
            httpd.shutdown()
            httpd.server_close()
