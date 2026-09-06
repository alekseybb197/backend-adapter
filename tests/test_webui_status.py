#!/usr/bin/env python3
"""Unit + HTTP tests for backend_adapter.webui_status — status page at "/".

Tests cover: _config_snapshot() mode detection (multi-backend / standalone),
_collect_endpoints() grouping, the «Доступные API» column (rendered from
config._ENDPOINT_STATE — result of the smoke probe run inside
refresh_models; a green ✓ is shown only for endpoints that answered HTTP
200 — failed probes are hidden), the background-check contract: GET "/"
renders the current state from config.refresh_state() (seeded as
_REFRESH_JOB here); the first GET — if no check has ever completed and
there is something to probe — auto-starts the FIRST check
(_autostart_first_check: adapter start / first GET / button); POST "/"
(the «⟳ Проверить сейчас» button) launches
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


def _http_post_body(port: int, path: str, body: bytes, content_type: str):
    """POST с телом (для JSON-запросов к /api/model-usage/reset).

    Возвращает (status, headers, body_text) — как _http_raw."""
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
        # непрошедший путь не рендерится в КОЛОНКЕ «Доступные API» — в теле
        # не должно быть ячейки «messages —»/«messages ✓»; слово messages
        # допустимо лишь в шапке секции «Использованные модели»
        assert ">messages ✓</span>" not in body
        assert ">messages —</span>" not in body
        assert "chat/completions ✓</span>" in body

    def test_render_status_400_hidden(self):
        # 400 — эндпоинт не прошёл проверку (found=False): в колонке его нет.
        config, ws = _fresh_modules()
        _seed_endpoint_state(config, "AAA", {
            "messages": {"found": False, "status": 400},
        })
        body = self._seed(config, ws)
        assert ">messages ✓</span>" not in body
        assert ">messages —</span>" not in body
        assert "✓</span>" not in body

    def test_render_unprobed_shows_not_probed(self):
        # Бэкенд вообще не пробован (нет записи в _ENDPOINT_STATE) — «не опрошено».
        config, ws = _fresh_modules()
        body = self._seed(config, ws)
        assert "не опрошено" in body

    def test_render_missing_endpoint_hidden(self):
        # Пропущенный эндпоинт (probe-модель не найдена → в результат не попал):
        # в ячейке «Доступные API» не показывается; виден только 200-путь.
        config, ws = _fresh_modules()
        _seed_endpoint_state(config, "AAA", {
            "chat/completions": {"found": True, "status": 200},
        })
        body = self._seed(config, ws)
        assert "chat/completions ✓" in body
        # пропущенные пути не рисуются как ячейки-«—» в колонке API
        assert ">messages —</span>" not in body
        assert ">responses —</span>" not in body
        assert ">embeddings —</span>" not in body

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
# TestUsageSection — таблица «Использованные модели» (v0.8.4)
# ---------------------------------------------------------------------------

def _seed_usage_rows(model_usage_mod, rows):
    """Заполнить таблицу model_usage строками напрямую (сид для рендера)."""
    model_usage_mod.reset_model_usage()
    for r in rows:
        model_usage_mod._TABLE[r["model"]] = r


class TestUsageSection:
    def _ctx(self):
        return mock.Mock(version="0.0.0-test")

    def _seed(self, config, ws, usage_rows=None):
        """Посев: один бэкенд + строки таблицы; вернуть полный HTML."""
        backend = {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"}
        config._BACKENDS = [backend]
        config._MODEL_TO_BACKEND = {"m-a": ("AAA", backend)}
        if usage_rows is not None:
            from backend_adapter import model_usage
            _seed_usage_rows(model_usage, usage_rows)
        return ws._render_status_page(self._ctx(), refresh=_done_job(True, 1)).decode()

    def _row(self, model, backend="AAA", calls=1, endpoints=None,
             input_tokens=0, output_tokens=0):
        return {
            "model": model,
            "backend": backend,
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "endpoints": endpoints or {},
            "errors": {},
            "first_seen": "10:00:00",
            "probing": False,
        }

    def test_section_present_with_headers(self):
        # Заголовок секции + 10 колонок (Модель|Бэкенд|Вызовов|Input|
        # Output|4 эндпоинта|Сброс).
        config, ws = _fresh_modules()
        body = self._seed(config, ws)
        assert "<h3 style=\"margin-top:24px\">Использованные модели</h3>" in body
        assert "<th>Модель</th>" in body
        assert "<th>Бэкенд</th>" in body
        assert "<th>Вызовов</th>" in body
        assert "<th>Input</th>" in body
        assert "<th>Output</th>" in body
        assert "<th>completions</th>" in body
        assert "<th>messages</th>" in body
        assert "<th>responses</th>" in body
        assert "<th>embeddings</th>" in body
        assert "<th>Сброс</th>" in body

    def test_empty_table_shows_placeholder(self):
        # Пустая таблица → строка «пока нет данных», никаких строк моделей.
        config, ws = _fresh_modules()
        body = self._seed(config, ws, usage_rows=[])
        assert "пока нет данных" in body
        assert "таблица заполняется при первых запросах к моделям" in body

    def test_row_renders_cells_and_marks(self):
        # found=True → зелёный ✓; found=False / не пробован → серая «—».
        config, ws = _fresh_modules()
        rows = [
            self._row("m-ok", endpoints={
                "completions": {"status": 200, "found": True},
                "messages": {"status": 404, "found": False},
                # responses/embeddings не пробованы — нет ключей
            }),
            self._row("m-none", endpoints={}),
        ]
        body = self._seed(config, ws, usage_rows=rows)
        assert "m-ok" in body and "m-none" in body
        # зелёный ✓ — только для found (один на обе строки)
        assert body.count('style="color:#1a7f37">✓</span>') == 1
        # серые «—»: m-ok (messages=404 + responses/embeddings не пробованы) = 3,
        # m-none (все 4 эндпоинта не пробованы) = 4 → всего 7; колонки
        # Input/Output «—» не дают (там «0»)
        assert body.count('style="color:#aaa">—</span>') == 7
        # строка m-none: Вызовов == 1 (счётчик из сида), токены 0/0
        assert ">m-none</td>" in body
        assert "<td>1</td>" in body  # calls строки m-none
        assert "<td>0</td>" in body  # Input строки m-none

    def test_escapes_model_and_backend_names(self):
        # Имя модели/бэкенда с HTML-спецсимволами не исполняется браузером.
        config, ws = _fresh_modules()
        rows = [
            self._row('<script>alert(1)</script>', backend='A&B'),
        ]
        body = self._seed(config, ws, usage_rows=rows)
        assert "&lt;script&gt;" in body
        assert "<script>alert" not in body
        assert "A&amp;B" in body

    def test_rows_in_insertion_order(self):
        # Строки — в порядке первого обращения (порядок usage_snapshot()).
        config, ws = _fresh_modules()
        rows = [self._row(f"m{i}") for i in range(3)]
        body = self._seed(config, ws, usage_rows=rows)
        assert body.index(">m0</td>") < body.index(">m1</td>") < body.index(">m2</td>")

    def test_section_after_backends_table(self):
        # Секция рендерится ПОСЛЕ основной таблицы бэкендов и до кнопки.
        config, ws = _fresh_modules()
        body = self._seed(config, ws, usage_rows=[self._row("m-a")])
        assert body.index("</table>") < body.index("Использованные модели")

    def test_cell_unit_found_and_missing(self):
        # _usage_endpoint_html: found → ✓, иначе (False/None) — «—».
        config, ws = _fresh_modules()
        assert "✓" in ws._usage_endpoint_html({"status": 200, "found": True})
        assert "—" in ws._usage_endpoint_html({"status": 404, "found": False})
        assert "—" in ws._usage_endpoint_html(None)

    def test_usage_rows_unit_empty_and_nonempty(self):
        # _usage_rows_html: 4 ячейки эндпоинтов на строку + плейсхолдер пустого.
        config, ws = _fresh_modules()
        html = ws._usage_rows_html([])
        assert "пока нет данных" in html and "<td colspan=\"10\"" in html
        row = self._row("m", endpoints={
            "completions": {"status": 200, "found": True},
        })
        html = ws._usage_rows_html([row])
        # модель+бэкенд+вызовов+input+output+4 эндпоинта+Сброс = 10
        assert html.count("<td>") == 10
        assert "completions" not in html  # короткие имена — только в шапке

    def test_row_renders_formatted_tokens(self):
        # input_tokens/output_tokens форматируются _fmt_tokens (с разделителем).
        config, ws = _fresh_modules()
        row = self._row("m-big", input_tokens=1536, output_tokens=12345)
        html = ws._usage_rows_html([row])
        assert "1 536" in html
        assert "12 345" in html

    def test_row_has_reset_form(self):
        # Каждая строка модели несёт form-кнопку «Сбросить» (POST на
        # /api/model-usage/reset?model=<имя>) — без JS, PRG через 303.
        config, ws = _fresh_modules()
        body = self._seed(config, ws, usage_rows=[self._row("m-reset")])
        assert "Сбросить" in body
        assert (
            '<form method="post" action="/api/model-usage/reset?model=m-reset">'
            in body
        )
        assert "color:#c0392b" in body  # красная ссылка-кнопка

    def test_empty_table_no_reset_forms(self):
        # У пустой таблицы (заглушка colspan=10) форм сброса нет (слово
        # «Сбросить» остаётся только в подписи-абзаце).
        config, ws = _fresh_modules()
        body = self._seed(config, ws, usage_rows=[])
        assert 'form method="post" action="/api/model-usage/reset' not in body
        assert "colspan=\"10\"" in body

    def test_reset_form_escapes_special_model_name(self):
        # Имя модели со спецсимволами: в action — quote(safe="")+html.escape,
        # спецсимволы не ломают query/атрибут и не исполняются браузером.
        config, ws = _fresh_modules()
        row = self._row('m & "x"/у=1')
        html = ws._usage_rows_html([row])
        from urllib.parse import quote as _quote
        q = _quote('m & "x"/у=1', safe="")
        assert f'action="/api/model-usage/reset?model={q}"' in html
        assert 'model=m & "' not in html  # сырые спецсимволы в action не выходят

    def test_page_header_is_backend_adapter(self):
        # Заголовок страницы — Backend-Adapter (не [CC]-adapter); внизу —
        # ссылка на GitHub-репозиторий проекта.
        config, ws = _fresh_modules()
        body = self._seed(config, ws)
        assert "Backend-Adapter — статус" in body
        assert "[CC]-adapter" not in body
        assert "https://github.com/alekseybb197/backend-adapter" in body


# ---------------------------------------------------------------------------
# TestFmtTokens — формат токенов «Input/Output» (точное число с разделителем)
# ---------------------------------------------------------------------------

class TestFmtTokens:
    def _fmt(self, n):
        config, ws = _fresh_modules()
        return ws._fmt_tokens(n)

    def test_small_exact_integers(self):
        # < 1000 — точное число без разделителя.
        assert self._fmt(0) == "0"
        assert self._fmt(1) == "1"
        assert self._fmt(999) == "999"

    def test_thousands_separator(self):
        # Разделитель тысяч (неразрывный узкий пробел) с 4-го разряда.
        assert self._fmt(1000) == "1 000"
        assert self._fmt(12345) == "12 345"
        assert self._fmt(1234567) == "1 234 567"

    def test_negative_treated_as_zero(self):
        # Отрицательных значений не бывает; защитно — как 0.
        assert self._fmt(-10) == "0"


# ---------------------------------------------------------------------------
# TestAutoStartFirstCheck — первый GET "/" запускает ПЕРВУЮ проверку
# ---------------------------------------------------------------------------

class TestAutoStartFirstCheck:
    """Прямые вызовы webui_status._autostart_first_check() (без HTTP)."""

    def test_no_endpoints_no_check(self):
        # Standalone без конфига: эндпойнтов нет — проверку не запускаем.
        config, ws = _fresh_modules()
        with mock.patch.object(config, "start_refresh", return_value=True) as m_start:
            assert ws._autostart_first_check() is False
        assert m_start.call_count == 0

    def test_first_check_runs(self):
        # Эндпойнты есть, проверок ещё не было (_REFRESH_JOB None): запускаем.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}
        with mock.patch.object(config, "start_refresh", return_value=True) as m_start:
            assert ws._autostart_first_check() is True
        assert m_start.call_count == 1
        assert m_start.call_args.kwargs.get("timeout") == ws.PROBE_TIMEOUT

    def test_running_does_not_start_second(self):
        # Проверка уже идёт (running=True): второй запуск не создаём.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}
        _seed_job(config, _running_job())
        with mock.patch.object(config, "start_refresh", return_value=True) as m_start:
            assert ws._autostart_first_check() is False
        assert m_start.call_count == 0

    def test_done_does_not_start_again(self):
        # Проверка уже завершалась (done_at есть): повторно не гоним —
        # дальше только по кнопке «⟳ Проверить сейчас».
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-alpha": ("AAA", config._BACKENDS[0])}
        _seed_job(config, _done_job(True, 1))
        with mock.patch.object(config, "start_refresh", return_value=True) as m_start:
            assert ws._autostart_first_check() is False
        assert m_start.call_count == 0

    def test_standalone_yaml_runs(self, tmp_path):
        # Standalone с YAML в ADAPTER_BACKEND_CONFIG: _BACKENDS пуст, но
        # эндпоинты читаются из YAML — первый заход запускает проверку.
        config, ws = _fresh_modules()
        config.ADAPTER_BACKEND_CONFIG = str(tmp_path / "backends.yaml")
        (tmp_path / "backends.yaml").write_text(
            "backend:\n"
            "  - name: AAA\n"
            '    base: "http://aaa.local"\n'
            "    key: k-aaa\n"
        )
        with mock.patch.object(config, "start_refresh", return_value=True) as m_start:
            assert ws._autostart_first_check() is True
        assert m_start.call_count == 1


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
        # Проверка уже завершалась (done-снимок в _REFRESH_JOB): GET "/"
        # показывает состояние последней проверки и НЕ запускает новую
        # (повторные проверки — только по кнопке).
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
        assert m_start.call_count == 0   # done-снимок — GET проверку не запускает
        assert m_refresh.call_count == 0

    def test_get_root_initial_state_starts_first_check(self, tmp_path):
        # Проверок ещё не было (_REFRESH_JOB None): первый GET "/"
        # запускает ПЕРВУЮ проверку автоматически (автостарт), страница
        # рендерится с баннером «Проверка выполняется». side_effect
        # публикует running-снимок, как настоящий start_refresh.
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
                status, body = _http_get(port, "/")
                assert status == 200
                assert "Проверка выполняется" in body
                assert "Проверить сейчас" in body
            finally:
                httpd.shutdown()
                httpd.server_close()
        assert m_start.call_count == 1
        assert m_start.call_args.kwargs.get("timeout") == ws.PROBE_TIMEOUT

    def test_get_root_running_state_does_not_start_check(self, tmp_path):
        # Проверка уже ИДЁТ (running=True в состоянии): GET "/" новую не
        # запускает — показывает баннер + поллинг (авто-релоад безопасен).
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

    def test_get_root_standalone_with_yaml_starts_check(self, tmp_path):
        # Standalone с YAML в ADAPTER_BACKEND_CONFIG (адаптер не стартовал,
        # _BACKENDS пуст, но эндпоинты читаются из YAML): первый GET "/"
        # запускает первую проверку (см. _autostart_first_check).
        config, ws = _fresh_modules()
        config.ADAPTER_BACKEND_CONFIG = str(tmp_path / "backends.yaml")
        (tmp_path / "backends.yaml").write_text(
            "backend:\n"
            "  - name: AAA\n"
            '    base: "http://aaa.local"\n'
            "    key: k-aaa\n"
        )
        with mock.patch.object(config, "start_refresh", return_value=False) as m_start:
            httpd, port = _start_server(str(tmp_path))
            try:
                status, body = _http_get(port, "/")
                assert status == 200
                assert "AAA" in body
            finally:
                httpd.shutdown()
                httpd.server_close()
        assert m_start.call_count == 1
        assert m_start.call_args.kwargs.get("timeout") == ws.PROBE_TIMEOUT

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
        with mock.patch.object(config, "start_refresh", return_value=False):
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
        with mock.patch.object(config, "start_refresh", return_value=False):
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
        with mock.patch.object(config, "start_refresh", return_value=False):
            httpd, port = _start_server(str(tmp_path))
            try:
                status, body = _http_get(port, "/")
                assert status == 200
                assert "<th>Endpoints</th>" in body  # заголовок колонки API
                assert "chat/completions ✓" in body
                # 404-путь в колонке API не рисуется (слово messages остаётся
                # только в шапке секции «Использованные модели»)
                assert ">messages —</span>" not in body
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
# /api/model-usage/reset: POST — сброс строки модели (PRG-кнопка + JSON API)
# ---------------------------------------------------------------------------

class TestModelUsageResetAPI:
    def _seed_row(self, model="m-reset"):
        from backend_adapter import model_usage
        _seed_usage_rows(model_usage, [{
            "model": model, "backend": "AAA", "calls": 5,
            "input_tokens": 0, "output_tokens": 0, "endpoints": {},
            "errors": {}, "first_seen": "10:00:00", "probing": False,
        }])

    def test_post_form_resets_and_redirects(self, tmp_path):
        # HTML-кнопка (без JSON Content-Type): POST ?model=m → 303 See Other
        # с Location "/" (PRG); после редиректа GET "/" строки нет.
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-reset": ("AAA", config._BACKENDS[0])}
        _seed_job(config, _done_job(True, 1))
        self._seed_row()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, headers, body = _http_post_body(
                port, "/api/model-usage/reset?model=m-reset", b"",
                "application/x-www-form-urlencoded",
            )
            assert status == 303
            assert headers.get("location") == "/"
            assert body == ""
            status2, body2 = _http_get(port, "/")
            assert status2 == 200
            assert ">m-reset</td>" not in body2
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_form_reset_then_row_gone_from_file(self, tmp_path):
        # Сброс формы реально удаляет строку и из YAML-файла (serve включил
        # persist на root_dir=tmp_path).
        config, ws = _fresh_modules()
        config._BACKENDS = [
            {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"},
        ]
        config._MODEL_TO_BACKEND = {"m-reset": ("AAA", config._BACKENDS[0])}
        _seed_job(config, _done_job(True, 1))
        from backend_adapter import model_usage
        _seed_usage_rows(model_usage, [{
            "model": "m-reset", "backend": "AAA", "calls": 5,
            "input_tokens": 0, "output_tokens": 0, "endpoints": {},
            "errors": {}, "first_seen": "10:00:00", "probing": False,
        }])
        httpd, port = _start_server(str(tmp_path))
        try:
            # принудительно сохранить (flush), чтобы строка была в файле
            model_usage._DIRTY = True  # сид пишет _TABLE напрямую, без флага
            model_usage.flush_table()
            _http_post_body(
                port, "/api/model-usage/reset?model=m-reset", b"",
                "application/x-www-form-urlencoded",
            )
            import yaml
            with open(str(tmp_path / model_usage.MODEL_USAGE_FILE), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            assert "m-reset" not in (data or {}).get("models", {})
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_json_ok_then_404(self, tmp_path):
        # JSON-клиент: 200 {"ok": true, ...}; повторный сброс — 404
        # {"error": ...} (строки уже нет).
        config, ws = _fresh_modules()
        self._seed_row()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, headers, body = _http_post_body(
                port, "/api/model-usage/reset?model=m-reset", b"",
                "application/json",
            )
            assert status == 200
            import json
            assert json.loads(body) == {"ok": True, "model": "m-reset"}
            status2, _, body2 = _http_post_body(
                port, "/api/model-usage/reset?model=m-reset", b"",
                "application/json",
            )
            assert status2 == 404
            assert "error" in json.loads(body2)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_json_missing_model_400(self, tmp_path):
        # Без query-параметра model — 400 {"error": ...} (JSON-клиент).
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _, body = _http_post_body(
                port, "/api/model-usage/reset", b"",
                "application/json",
            )
            assert status == 400
            import json
            assert "error" in json.loads(body)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_post_form_without_model_redirects(self, tmp_path):
        # HTML-форма без model — 303 на "/" (в норме невозможно: кнопка
        # всегда несёт model).
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, headers, body = _http_post_body(
                port, "/api/model-usage/reset", b"",
                "application/x-www-form-urlencoded",
            )
            assert status == 303
            assert headers.get("location") == "/"
            assert body == ""
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_get_returns_404(self, tmp_path):
        # GET на префикс сброса — 404 (дефолт Endpoint; сброс только POST).
        config, ws = _fresh_modules()
        httpd, port = _start_server(str(tmp_path))
        try:
            status, _ = _http_get(port, "/api/model-usage/reset?model=m")
            assert status == 404
        finally:
            httpd.shutdown()
            httpd.server_close()


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
