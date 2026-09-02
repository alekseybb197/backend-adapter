#!/usr/bin/env python3
"""
webui_status.py — эндпойнт "/" общего веб-сервера WEBUI: статус-страница.

Показывает на одной странице:
  - версию кода (из WebContext.version — в адаптере это __version__ из
    backend-adapter.py, единственный источник);
  - режим работы (legacy single-backend / multi-backend / standalone);
  - каждый настроенный LLM-эндпойнт: доступность и список моделей.

Откуда данные:
  - GET "/" рендерит СОСТОЯНИЕ НА СТАРТЕ: snapshot конфиг-глобалов
    адаптера (_BACKENDS/_BACKEND_BY_NAME/_MODEL_TO_BACKEND/_AVAILABLE_MODELS
    для multi-backend; BACKEND_BASE + _AVAILABLE_MODELS для legacy) —
    загрузка мгновенная, без сетевых запросов. Это те же данные, на
    которых адаптер уже работает (модели опрашивались при старте), —
    дублирующая живая проба на каждый заход страницы была бы лишней
    нагрузкой и задержкой.
  - POST "/" (кнопка «⟳ Проверить сейчас») выполняет ЖИВУЮ пробу каждого
    эндпойнта через _probe_live() с жёстким коротким таймаутом 5 с на
    эндпойнт и рендерит ту же страницу со свежими статусами и временем
    проверки. Прямого переиспользования config._fetch_models нет: он
    наследует ADAPTER_TIMEOUT (по умолчанию 300 с) и печатает
    [FETCH_ERROR] в stdout — для страницы нужен собственный короткий
    таймаут и возврат ошибки значением, а не raise/print (config.py не
    трогаем).

САМОСТОЯТЕЛЬНЫЙ ЗАПУСК (standalone — python -m backend_adapter.webserver
вне процесса адаптера): конфиг-глобалы адаптера пусты (нет ни env
бэкенда, ни YAML-конфига), поэтому страница показывает режим standalone
и подсказку, как получить живые данные, — вместо фантомного списка
«example.com» из дефолтов env. Эндпойнты для живой пробы в этом режиме
могут быть заданы переменными окружения адаптера (ADAPTER_BACKEND_BASE +
ADAPTER_BACKEND_KEY для одного, либо ADAPTER_BACKEND_CONFIG для
нескольких) — модуль берёт их из config так же, как сам адаптер.

Чистая логика (snapshot, проба) вынесена в отдельные функции, чтобы её
можно было тестировать без HTTP-сервера.
"""
import html
import json
import logging
import os
import time
import urllib.request

from . import config
from . import webserver

logger = logging.getLogger("webui_status")

PROBE_TIMEOUT = 5.0  # жёсткий таймаут живой пробы одного эндпойнта, сек
MODEL_STRICT_CAP = 60  # потолок выводимых моделей на эндпойнт (не резать страницу)


# ==================== ЧИСТАЯ ЛОГИКА ====================


def _collect_endpoints() -> list[dict]:
    """Список настроенных LLM-эндпойнтов из конфиг-глобалов адаптера.

    Каждый элемент: {"name", "base", "key", "models": [model_id, ...]}.
    models — модели, успешно опрошенные на старте адаптера (для
    multi-backend — из _MODEL_TO_BACKEND, сгруппированные по бэкенду; это
    ровно те модели, что адаптер реально принимает в запросах). key —
    токен для живой пробы (в HTML не выводится).

    ВАЖНО про standalone: config.BACKEND_BASE не пуст даже без env —
    в config.py у него дефолт "https://llm.service.example.com". Поэтому
    legacy-эндпойнт добавляется только если ADAPTER_BACKEND_BASE РЕАЛЬНО
    задана в окружении процесса — иначе страница показала бы фантомный
    «example.com» вместо честного «данных нет».

    Режимы:
      - multi-backend в процессе адаптера (_BACKENDS заполнен при старте);
      - legacy single-backend (BACKEND_BASE/BACKEND_KEY из env; модели —
        ключи _AVAILABLE_MODELS после стартовой пробы);
      - standalone (viewer вне адаптера): эндпойнты не опрошены, но если
        окружение задаёт бэкенд (ADAPTER_BACKEND_BASE или
        ADAPTER_BACKEND_CONFIG с YAML) — они показываются пустыми, чтобы
        кнопка «⟳ Проверить сейчас» могла выполнить живую пробу."""
    endpoints = []

    if config._BACKENDS:
        # multi-backend: явный список бэкендов из YAML, загружен адаптером
        for b in config._BACKENDS:
            endpoints.append({
                "name": b["name"],
                "base": b["base"],
                "key": b["key"],
                "models": sorted(mid for mid, (bname, _) in
                                 config._MODEL_TO_BACKEND.items() if bname == b["name"]),
            })
        return endpoints

    if not config._BACKEND_LEGACY:
        # ADAPTER_BACKEND_CONFIG задан, но _BACKENDS пуст — адаптер в этом
        # процессе не стартовал (standalone, либо упал до инициализации).
        # Перечитываем YAML только ради списка эндпойнтов для живой пробы
        # (парсер config._parse_backend_yaml — тот же, что у адаптера;
        # модели всё равно не опрошены — статус будет «не опрошен»).
        cfg_path = config.ADAPTER_BACKEND_CONFIG or os.environ.get("ADAPTER_BACKEND_CONFIG", "")
        if cfg_path and os.path.isfile(cfg_path):
            blocks = config._parse_backend_yaml(cfg_path)
            for b in blocks or []:
                endpoints.append({"name": b["name"], "base": b["base"],
                                  "key": b["key"], "models": []})
        return endpoints

    # legacy single-backend: бэкенд есть только если env-переменная задана
    # реально (иначе BACKEND_BASE — дефолт config.py, показывать нечего)
    if os.environ.get("ADAPTER_BACKEND_BASE"):
        endpoints.append({
            "name": "legacy",
            "base": config.BACKEND_BASE,
            "key": config.BACKEND_KEY,
            "models": sorted(config._AVAILABLE_MODELS.keys()),
        })
    return endpoints


def _config_snapshot() -> dict:
    """Состояние на старте для GET "/": режим, эндпойнты, доступность.

    Возвращает {"mode": str, "endpoints": [{"name","base","key","models",
    "status"}], "note": str|None}. status — "ok" (на старте опрошен, есть
    модели) или "не опрошен" (бэкенд есть в конфиге, но моделей нет — в
    multi-режиме это бэкенд, чья проба не удалась; в standalone — любой
    бэкенд из env, т.к. проб никто не делал). Пустой список эндпойнтов —
    режим standalone без env-бэкенда: страница показывает подсказку."""
    endpoints = _collect_endpoints()
    if config._BACKENDS:
        mode = "multi-backend"
    elif not config._BACKEND_LEGACY and endpoints:
        # standalone с ADAPTER_BACKEND_CONFIG: бэкендов несколько, но без
        # стартовой инициализации адаптера
        mode = "multi-backend"
    elif endpoints:
        mode = "legacy"
    else:
        mode = "standalone"

    for ep in endpoints:
        ep["status"] = "ok" if ep["models"] else "не опрошен"

    note = None
    if not endpoints:
        note = ("Данные адаптера недоступны — запущен standalone-режим (viewer вне процесса "
                "адаптера). Живые данные появятся после запуска внутри адаптера "
                "(ADAPTER_WEBUI_ENABLE=1), либо задайте env-переменные бэкенда "
                "(ADAPTER_BACKEND_BASE/ADAPTER_BACKEND_KEY или ADAPTER_BACKEND_CONFIG) "
                "и перезапустите сервер.")
    return {"mode": mode, "endpoints": endpoints, "note": note}


def _probe_live(base: str, key: str, timeout: float = PROBE_TIMEOUT):
    """Живая проба LLM-эндпойнта: GET base/v1/models.

    Возвращает (ok: bool, payload): payload — список моделей [{"id": ...}]
    при успехе, либо строка с текстом ошибки. НЕ бросает исключений и НЕ
    печатает в stdout (в отличие от config._fetch_models) — страница
    должна пережить падение любого эндпойнта. Таймаут — собственный
    короткий (по умолчанию 5 с), чтобы упавший бэкенд не вешал страницу
    на ADAPTER_TIMEOUT (300 с)."""
    url = base.rstrip("/") + "/v1/models"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Connection": "keep-alive"},
        method="GET",
    )
    try:
        resp = urllib.request.urlopen(req, context=config.SSL_CTX, timeout=timeout)
        raw = resp.read()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return False, f"не JSON: {e}"
        models = data if isinstance(data, list) else data.get("data", [])
        return True, models
    except Exception as e:  # недоступность, DNS, таймаут, HTTP-ошибка
        return False, str(e)


# ==================== HTML-РЕНДЕР ====================


def _models_html(models: list[str], status: str) -> str:
    if not models:
        return f'<span style="color:#999">{status}</span>'
    if len(models) <= MODEL_STRICT_CAP:
        return ", ".join(html.escape(m) for m in models)
    shown = ", ".join(html.escape(m) for m in models[:MODEL_STRICT_CAP])
    return f"{shown} <span style='color:#999'>(+{len(models) - MODEL_STRICT_CAP} ещё)</span>"


def _render_status_page(context, live_results=None, checked_at=None) -> bytes:
    """HTML статус-страницы.

    live_results — результат POST-пробы: {base: (ok, payload)} (или None —
    рендер состояния на старте). checked_at — человекочитаемое время
    последней живой проверки (или None)."""
    snapshot = _config_snapshot()
    endpoints = snapshot["endpoints"]

    if not endpoints and live_results is None:
        # standalone без env бэкенда: попробовать показать хоть что-то
        # живое можно только если окружение задано — тогда _collect_endpoints
        # уже вернул бы записи; здесь действительно данных нет.
        pass

    rows = []
    for ep in endpoints:
        if live_results is not None and ep["base"] in live_results:
            ok, payload = live_results[ep["base"]]
            if ok:
                status = "ok"
                models = sorted(m.get("id", "") for m in payload)
                status_cell = f'<span style="color:#1a7f37">ok</span>'
            else:
                status = "ошибка"
                models = []
                status_cell = (f'<span style="color:#c0392b">недоступен</span> '
                               f'<span style="color:#999">({html.escape(str(payload))})</span>')
            models_cell = _models_html(models, status)
        else:
            status_cell = (f'<span style="color:#1a7f37">ok</span>'
                           if ep["status"] == "ok" else
                           f'<span style="color:#b8860b">{html.escape(ep["status"])}</span>')
            models_cell = _models_html(ep["models"], ep["status"])

        rows.append(f"""
      <tr>
        <td>{html.escape(ep["name"])}</td>
        <td><code>{html.escape(ep["base"])}</code></td>
        <td>{status_cell}</td>
        <td>{models_cell}</td>
      </tr>""")

    if not endpoints:
        rows.append("""
      <tr><td colspan="4" style="color:#888">нет данных (см. примечание ниже)</td></tr>""")

    if live_results is not None:
        footer = (f'<p style="color:#555">Живая проверка выполнена в '
                  f'{html.escape(checked_at or "")}.</p>')
    else:
        footer = ('<p style="color:#888">Показано состояние на старте адаптера. '
                  'Кнопка выполняет живую проверку каждого эндпойнта '
                  '(GET /v1/models, таймаут 5 с).</p>')

    note_html = (f'<p style="color:#b8860b">{html.escape(snapshot["note"])}</p>'
                 if snapshot["note"] else "")

    html_page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>backend-adapter — статус</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 24px; color: #222; }}
  table {{ border-collapse: collapse; margin-top: 12px; }}
  td, th {{ border: 1px solid #ddd; padding: 6px 12px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; }}
  code {{ font-size: 13px; }}
</style>
</head>
<body>
<h2>[CC]-adapter — статус</h2>
<p><b>Версия кода:</b> {html.escape(context.version)} &nbsp;·&nbsp;
   <b>Режим:</b> {html.escape(snapshot["mode"])} &nbsp;·&nbsp;
   <a href="/session">просмотр сессий →</a></p>
{note_html}
<table>
  <tr><th>Эндпойнт</th><th>Base URL</th><th>Статус</th><th>Модели</th></tr>
  {''.join(rows)}
</table>
{footer}
<form method="POST" action="/" style="margin-top:12px">
  <button type="submit">⟳ Проверить сейчас</button>
</form>
</body>
</html>
"""
    return html_page.encode("utf-8")


# ==================== ЭНДПОЙНТ ====================


@webserver.register
class StatusEndpoint(webserver.Endpoint):
    """Эндпойнт "/": статус-страница (версия кода, эндпойнты LLM, модели).

    GET — состояние на старте (snapshot конфиг-глобалов, без сети);
    POST — живая проба всех эндпойнтов с коротким таймаутом и рендер той
    же страницы со свежими данными."""

    prefix = "/"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        if remainder:
            handler.send_error(404, "Not found")
            return
        handler._write(200, "text/html; charset=utf-8",
                       _render_status_page(self.context))

    def POST(self, handler, remainder: str):
        # Тело формы не читаем — кнопка одна, действий нет.
        results = {}
        for ep in _collect_endpoints():
            ok, payload = _probe_live(ep["base"], ep["key"])
            results[ep["base"]] = (ok, payload)
        checked_at = time.strftime("%H:%M:%S")
        handler._write(200, "text/html; charset=utf-8",
                       _render_status_page(self.context, live_results=results,
                                           checked_at=checked_at))


__all__ = [
    "PROBE_TIMEOUT",
    "MODEL_STRICT_CAP",
    "_collect_endpoints",
    "_config_snapshot",
    "_probe_live",
    "_render_status_page",
    "StatusEndpoint",
]
