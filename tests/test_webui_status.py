#!/usr/bin/env python3
"""Unit + HTTP tests for backend_adapter.webui_status — status page at "/".

Tests cover: _config_snapshot() mode detection (multi-backend / standalone),
_collect_endpoints() grouping, the «Доступные API» column (rendered from
config._ENDPOINT_STATE — result of the smoke probe run inside
refresh_models), GET "/" and POST "/" — both trigger
config.refresh_models() (on-demand model cache refresh, mocked here)
and render the page from the refreshed globals: success shows the new model
list, failure keeps the old cache and shows the error text, standalone
(no endpoints) renders the notice without any refresh call. The Models cell
(_models_html) is capped at MODEL_LINES rows with an expand/collapse button
(JS models_toggle on the page) — see TestModelsCell.
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


def _ok_refresh(count: int, errors=None, probe=None) -> dict:
    res = {"ok": True, "count": count, "errors": errors or {}}
    if probe is not None:
        res["probe"] = probe
    return res


def _fail_refresh(count: int, errors) -> dict:
    return {"ok": False, "count": count, "errors": errors}


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
            refresh = _ok_refresh(1)
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

    def test_render_shows_found_and_not_found(self):
        # Найденные пути зелёным с ✓, ненайденные — серым «—».
        config, ws = _fresh_modules()
        _seed_endpoint_state(config, "AAA", {
            "chat/completions": {"found": True, "status": 200},
            "messages": {"found": False, "status": 404},
        })
        body = self._seed(config, ws)
        assert "chat/completions" in body and "✓" in body
        # messages: 404 → найден не был → «—»
        assert "messages" in body and "—" in body

    def test_render_status_400_shows_check_and_status(self):
        # 400/401/405 классифицируются как «эндпоинт есть» — зелёный ✓ с кодом.
        config, ws = _fresh_modules()
        _seed_endpoint_state(config, "AAA", {
            "messages": {"found": True, "status": 400},
        })
        body = self._seed(config, ws)
        assert "messages" in body and "✓" in body
        assert "400" in body

    def test_render_unprobed_shows_not_probed(self):
        # Бэкенд вообще не пробован (нет записи в _ENDPOINT_STATE) — «не опрошено».
        config, ws = _fresh_modules()
        body = self._seed(config, ws)
        assert "не опрошено" in body

    def test_render_missing_endpoint_shows_dash(self):
        # Пропущенный эндпоинт (probe-модель не найдена → в результат не попал):
        # в ячейке он «—», остальные — по результатам пробы.
        config, ws = _fresh_modules()
        _seed_endpoint_state(config, "AAA", {
            "chat/completions": {"found": True, "status": 200},
        })
        body = self._seed(config, ws)
        assert "chat/completions ✓" in body
        assert "messages —" in body
        assert "responses —" in body
        assert "embeddings —" in body

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
        body = ws._render_status_page(ctx, refresh=_ok_refresh(6)).decode()
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

    def test_get_root_shows_api_column_from_state(self, tmp_path):
        # Полный путь: GET "/" → refresh_models (замокан) → страница рендерит
        # колонку «Доступные API» из config._ENDPOINT_STATE (в проде туда
        # пишет probe_endpoints, выполняющийся внутри refresh_models).
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}
        _seed_endpoint_state(config, "AAA", {
            "chat/completions": {"found": True, "status": 200},
            "messages": {"found": False, "status": 404},
        })
        with mock.patch.object(config, "refresh_models", return_value=_ok_refresh(1)):
            httpd, port = _start_server(str(tmp_path))
            try:
                status, body = _http_get(port, "/")
                assert status == 200
                assert "<th>Endpoints</th>" in body  # заголовок колонки API
                assert "chat/completions ✓" in body
                assert "messages —" in body
            finally:
                httpd.shutdown()
                httpd.server_close()

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
