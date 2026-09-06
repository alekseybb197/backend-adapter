#!/usr/bin/env python3
"""
webui_status.py — эндпойнт "/" общего веб-сервера WEBUI: статус-страница.

Показывает на одной странице:
  - версию кода (из WebContext.version — в адаптере это __version__ из
    backend-adapter.py, единственный источник);
  - режим работы (multi-backend / standalone);
  - каждый настроенный LLM-эндпойнт: доступность, список моделей и
    колонку «Доступные API» — какие известные API-эндпойнты бэкенд реально
    обслуживает (результат дымовой пробы config.probe_endpoints, см. ниже);
  - таблицу «Использованные модели» — модели, к которым агент обращался
    после старта адаптера (см. model_usage.py): счётчик обращений и какие
    API-эндпоинты доступны именно для каждой модели (результат дымовой
    пробы этой моделью при первом обращении).

Откуда данные:
  - Проверка бэкендов запускается:
      * при старте адаптера (backend-adapter.py запускает config.
        start_refresh(timeout=PROBE_TIMEOUT) после поднятия WEBUI) и
      * по кнопке «⟳ Проверить сейчас» (POST "/") — работает по
        PRG-паттерну: запускает ФОНОВУЮ проверку config.start_refresh(
        timeout=PROBE_TIMEOUT) (см. config._refresh_worker) и отвечает
        303 See Other на GET "/" — браузер переходит на страницу
        GET-навигацией, HTTP-запрос не ждёт завершения проверки.
        Поэтому обновление/авто-релоад страницы никогда не повторяет
        POST (нет диалога «повторить действие?»).
  - Первый заход на страницу (GET "/", _autostart_first_check)
    запускает ПЕРВУЮ проверку автоматически, если проверок ещё не
    было (done_at пуст) и есть что проверять; повторные проверки —
    только по кнопке. Пока проверка выполняется, страница показывает
    баннер «Проверка выполняется…» и опрашивает лёгкий JSON-эндпоинт
    /api/refresh-state; по завершении проверки JS сам перезагружает
    страницу (location.reload()) — она рендерит свежий результат.
    Авто-релоад безопасен: он происходит на GET-документе, повторного
    POST нет, зацикливания нет.
  - Страница рендерится из конфиг-глобалов адаптера: бэкенд мог добавить
    новые модели между стартами адаптера (или после ошибки 400 «model is
    not available»), refresh подхватывает их без перезапуска — следующие
    запросы /v1/messages с новыми моделями проходят строгую валидацию.
  - Футер «Список моделей обновлён в HH:MM:SS» и статусы строк берутся из
    состояния последней проверки (config.refresh_state(): ok/count/errors/
    checked_at); при полном провале старый список моделей сохраняется и
    показывается на странице вместе с текстом ошибки; при частичном успехе
    страница показывает свежий список ответивших бэкендов и тексты ошибок
    упавших.
  - Дымовая проба API: refresh_models дополнительно несёт "probe" —
    результат config.probe_endpoints(): какие из известных эндпойнтов
    (/v1/chat/completions, /v1/messages, /v1/responses, /v1/embeddings)
    бэкенд обслуживает. Проба — короткие POST с max_tokens:1 (модель —
    из необязательного ключа probe YAML-записи бэкенда, либо первая из
    /v1/models), результат кэшируется ~60 с (ENDPOINT_PROBE_TTL) и
    отключается флагом ADAPTER_ENDPOINT_PROBE=0. Для каждого бэкенда
    колонка «Доступные API» показывает зелёным ✓ только реально
    работающие пути (HTTP 200); непрошедшие проверку пути на странице
    не показываются (из config._ENDPOINT_STATE).

САМОСТОЯТЕЛЬНЫЙ ЗАПУСК (standalone — python -m backend_adapter.webserver
вне процесса адаптера): конфиг-глобалы адаптера пусты (нет YAML-конфига),
поэтому страница показывает режим standalone и подсказку, как получить
живые данные, — вместо фантомного списка «example.com» из дефолтов env.
Эндпойнты для проверки в этом режиме задаются той же переменной
окружения, что у адаптера, — ADAPTER_BACKEND_CONFIG (путь к YAML);
модуль берёт её из config так же, как сам адаптер.

Чистая логика (snapshot, состояние, рендер) вынесена в отдельные функции,
чтобы её можно было тестировать без HTTP-сервера.
"""

import html
import json
import logging
import os
import time

from . import config, model_usage, webserver

logger = logging.getLogger("webui_status")

PROBE_TIMEOUT = 5.0  # жёсткий таймаут живой пробы одного эндпойнта, сек


# ==================== ЧИСТАЯ ЛОГИКА ====================


def _collect_endpoints() -> list[dict]:
    """Список настроенных LLM-эндпойнтов из конфиг-глобалов адаптера.

    Каждый элемент: {"name", "base", "key", "models": [model_id, ...],
    "api": {"путь_без_/v1": {"found": bool, "status": int|None}} | None}.
    models — модели, успешно опрошенные на старте адаптера (из
    _MODEL_TO_BACKEND, сгруппированные по бэкенду; это ровно те модели,
    что адаптер реально принимает в запросах). key — токен для живой
    пробы (в HTML не выводится). api — результат дымовой пробы эндпойнтов
    из config._ENDPOINT_STATE (нормализован: ключи без префикса "/v1/",
    в порядке config.ENDPOINT_PROBES); None — бэкенд ещё не пробован
    (standalone: refresh не делался; ADAPTER_ENDPOINT_PROBE=0).

    Режимы:
      - multi-backend в процессе адаптера (_BACKENDS заполнен при старте);
      - standalone (viewer вне адаптера): эндпойнты не опрошены, но если
        окружение задаёт ADAPTER_BACKEND_CONFIG с YAML-файлом — они
        показываются пустыми, чтобы кнопка «⟳ Проверить сейчас» могла
        выполнить живую пробу."""
    endpoints = []

    if config._BACKENDS:
        # Бэкенды из YAML, загружен адаптером при старте
        for b in config._BACKENDS:
            api = None
            ep_state = config._ENDPOINT_STATE.get(b["name"])
            if ep_state and ep_state.get("endpoints"):
                # Ключи результата — полные пути ("/v1/chat/completions");
                # нормализуем до "chat/completions" (без "/v1/") — рендер
                # сам восстановит порядок из ENDPOINT_PROBES.
                api = {path[len("/v1/") :]: ep for path, ep in ep_state["endpoints"].items()}
            endpoints.append(
                {
                    "name": b["name"],
                    "base": b["base"],
                    "key": b["key"],
                    "models": sorted(
                        mid
                        for mid, (bname, _) in config._MODEL_TO_BACKEND.items()
                        if bname == b["name"]
                    ),
                    "api": api,
                }
            )
        return endpoints

    # ADAPTER_BACKEND_CONFIG задан, но _BACKENDS пуст — адаптер в этом
    # процессе не стартовал (standalone, либо упал до инициализации).
    # Перечитываем YAML только ради списка эндпойнтов для живой пробы
    # (парсер config._parse_backend_yaml — тот же, что у адаптера;
    # модели всё равно не опрошены — статус будет «не опрошен»).
    cfg_path = config.ADAPTER_BACKEND_CONFIG or os.environ.get("ADAPTER_BACKEND_CONFIG", "")
    if cfg_path and os.path.isfile(cfg_path):
        blocks = config._parse_backend_yaml(cfg_path)
        for b in blocks or []:
            endpoints.append(
                {
                    "name": b["name"],
                    "base": b["base"],
                    "key": b["key"],
                    "models": [],
                    "api": None,
                }
            )
    return endpoints


def _config_snapshot() -> dict:
    """Состояние на старте для GET "/": режим, эндпойнты, доступность.

    Возвращает {"mode": str, "endpoints": [{"name","base","key","models",
    "status"}], "note": str|None}. status — "ok" (на старте опрошен, есть
    модели) или "не опрошен" (бэкенд есть в конфиге, но моделей нет — в
    multi-режиме это бэкенд, чья проба не удалась; в standalone — любой
    бэкенд из конфига, т.к. проб никто не делал). Пустой список
    эндпойнтов — режим standalone без конфига: страница показывает
    подсказку."""
    endpoints = _collect_endpoints()
    mode = "multi-backend" if endpoints else "standalone"

    for ep in endpoints:
        ep["status"] = "ok" if ep["models"] else "не опрошен"

    note = None
    if not endpoints:
        note = (
            "Данные адаптера недоступны — запущен standalone-режим (viewer вне процесса "
            "адаптера). Живые данные появятся после запуска внутри адаптера "
            "(ADAPTER_WEBUI_ENABLE=1), либо задайте ADAPTER_BACKEND_CONFIG (путь к "
            "YAML-файлу конфигурации бэкенда) и перезапустите сервер."
        )
    return {"mode": mode, "endpoints": endpoints, "note": note}


# ==================== HTML-РЕНДЕР ====================


MODEL_LINES = 4  # высота свёрнутого списка моделей: строк (каждый id — своя строка)


def _models_html(models: list[str], status: str) -> str:
    """HTML ячейки «Models»: каждый id модели — отдельная строка.

    Если моделей больше MODEL_LINES — первые MODEL_LINES показываются,
    остальные прячутся в <span class="models-extra" style="display:none">,
    а кнопка «Показать ещё (N)» разворачивает список (JS models_toggle,
    см. _render_status_page): при клике span получает display:block,
    кнопка меняется на «Свернуть» и прячет его обратно."""
    if not models:
        return f'<span style="color:#999">{status}</span>'
    line = '<div style="line-height:1.5">{}</div>'
    if len(models) <= MODEL_LINES:
        return "".join(line.format(html.escape(m)) for m in models)
    shown = "".join(line.format(html.escape(m)) for m in models[:MODEL_LINES])
    extra = "".join(line.format(html.escape(m)) for m in models[MODEL_LINES:])
    n = len(models) - MODEL_LINES
    btn = (
        f'<button type="button" onclick="models_toggle(this)" '
        f'data-models-count="{len(models)}" '
        f'style="color:#1a7f37;background:none;border:none;padding:0;'
        f'font:inherit;cursor:pointer;text-decoration:underline">'
        f"Показать ещё ({n})</button>"
    )
    return f'{shown}<span class="models-extra" style="display:none">{extra}</span>{btn}'


def _api_html(api: dict | None) -> str:
    """HTML ячейки «Доступные API» для одного бэкенда.

    ``api`` — результат дымовой пробы из _collect_endpoints()
    ({короткий_путь: {"found": bool, "status": int|None}}), None — бэкенд
    ещё не пробован (standalone / ADAPTER_ENDPOINT_PROBE=0). Зелёным с ✓
    показываются ТОЛЬКО реально работающие эндпоинты (found=True ⇔ HTTP
    200, см. классификацию в config._probe_backend_endpoints); непрошедшие
    (любой не-200 код, сетевая ошибка) и пропущенные (нет probe-модели —
    в api не значатся) на странице НЕ показываются вовсе. Проба была
    (api не None), но ни один эндпоинт не ответил 200 — ячейка показывает
    один серый «—» с пояснением вместо пустого места. Порядок —
    config.ENDPOINT_PROBES (тот же, что у самой пробы)."""
    if api is None:
        return '<span style="color:#999">не опрошено</span>'
    parts = []
    for _pname, path, _tpl in config.ENDPOINT_PROBES:
        label = path[len("/v1/") :]
        ep = api.get(label)
        if ep is not None and ep["found"]:
            # found=True гарантированно значит HTTP 200 (см. config.py)
            parts.append(f'<span style="color:#1a7f37">{label} ✓</span>')
    if not parts:
        parts.append(
            '<span style="color:#aaa" title="ни один эндпоинт не ответил HTTP 200">—</span>'
        )
    return "<br>".join(parts)


def _usage_endpoint_html(entry: dict | None) -> str:
    """HTML ячейки эндпоинта в таблице «Использованные модели».

    ``entry`` — элемент ``endpoints[pname]`` строки таблицы model_usage:
    {"status": int|None, "found": bool} (found ⇔ HTTP 200); None — эндпоинт
    не пробован (первый запрос ещё идёт — probing, или ADAPTER_MODEL_USAGE_
    ENABLE=0, или модели нет у бэкенда). Зелёный ``✓`` — только found=True;
    всё остальное (не-200, не пробован) — серая ``—`` (провал пробы детален
    в логе/строке, страница — про факт доступности)."""
    if entry is not None and entry["found"]:
        return '<span style="color:#1a7f37">✓</span>'
    return '<span style="color:#aaa">—</span>'


def _usage_rows_html(rows: list[dict]) -> str:
    """HTML строк таблицы «Использованные модели» (по строке на модель).

    ``rows`` — model_usage.usage_snapshot() (порядок первого обращения).
    Колонки: Модель | Бэкенд | Вызовов | эндпоинты ENDPOINT_PROBES
    (completions/messages/responses/embeddings — в порядке config.
    ENDPOINT_PROBES, том же, что у пробы). Модель/бэкенд — html.escape."""
    body = []
    for r in rows:
        ep_cells = "".join(
            f"<td>{_usage_endpoint_html(r['endpoints'].get(pname))}</td>"
            for pname, _path, _tpl in config.ENDPOINT_PROBES
        )
        body.append(
            "<tr>"
            f"<td>{html.escape(str(r['model']))}</td>"
            f"<td>{html.escape(str(r['backend']))}</td>"
            f"<td>{r['calls']}</td>"
            f"{ep_cells}"
            "</tr>"
        )
    if not body:
        body.append(
            '<tr><td colspan="7" style="color:#888">пока нет данных — '
            "таблица заполняется при первых запросах к моделям</td></tr>"
        )
    return "".join(body)


def _render_status_page(
    context, refresh=None, checked_at=None, running=None, started_at=None
) -> bytes:
    """HTML статус-страницы.

    ``refresh`` — результат последней проверки из config.refresh_state():
    {"ok", "count", "errors": {имя_бэкенда: текст}} (или None — когда
    проверок ещё не было / обновлять было нечего: standalone без
    настроенных эндпойнтов). checked_at — время последнего обновления
    ("HH:MM:SS", или None). running — идёт ли проверка прямо сейчас
    (показывается баннер). started_at — time.time() запуска идущей
    проверки (для времени в баннере; None, если проверка не идёт).

    Статусы строк берутся из snapshot, но поверх: если бэкенд есть в
    refresh["errors"] — строка показывает «недоступен (текст ошибки)».
    Модели — из обновлённых конфиг-глобалов: при полном провале refresh
    кэш не тронут (показывается прежний список), при частичном — упавший
    бэкенд честно без моделей. Колонка «Доступные API» рендерится из
    config._ENDPOINT_STATE через _collect_endpoints (сама проба выполняется
    внутри refresh_models; refresh["probe"] отдельно не рендерится).
    Секция «Использованные модели» рендерится из model_usage.usage_snapshot()
    (см. _usage_rows_html) — таблица заполняется запросами агента в этом
    процессе независимо от проверок бэкендов."""
    snapshot = _config_snapshot()
    endpoints = snapshot["endpoints"]
    errors = (refresh or {}).get("errors", {}) or {}

    rows = []
    for ep in endpoints:
        err_text = errors.get(ep["name"])
        if err_text:
            status_cell = (
                f'<span style="color:#c0392b">недоступен</span> '
                f'<span style="color:#999">({html.escape(str(err_text))})</span>'
            )
            # Модели: после refresh кэш отражает реальность — упавший
            # бэкенд при частичном успехе выпал из кэша (моделей нет);
            # при полном провале кэш не тронут и показывается прежний.
            models_cell = _models_html(ep["models"], "недоступен")
        else:
            status_cell = (
                '<span style="color:#1a7f37">ok</span>'
                if ep["status"] == "ok"
                else f'<span style="color:#b8860b">{html.escape(ep["status"])}</span>'
            )
            models_cell = _models_html(ep["models"], ep["status"])

        rows.append(f"""
      <tr>
        <td>{html.escape(ep["name"])}</td>
        <td><code>{html.escape(ep["base"])}</code></td>
        <td>{status_cell}</td>
        <td>{_api_html(ep.get("api"))}</td>
        <td>{models_cell}</td>
      </tr>""")

    if not endpoints:
        rows.append("""
      <tr><td colspan="5" style="color:#888">нет данных (см. примечание ниже)</td></tr>""")

    if refresh is None:
        footer = (
            '<p style="color:#888">Список моделей и API-эндпойнты бэкендов '
            "проверяются по кнопке «⟳ Проверить сейчас» (GET /v1/models + "
            "дымовые POST max_tokens:1, таймаут 5 с на эндпойнт; проба "
            "кэшируется 60 с, ADAPTER_ENDPOINT_PROBE=0 — отключить). "
            "Первый заход на страницу запускает первую проверку "
            "автоматически; повторные — только по кнопке.</p>"
        )
    else:
        count = refresh.get("count", 0)
        if refresh.get("ok"):
            base = (
                f'<span style="color:#1a7f37">Список моделей обновлён '
                f"в {html.escape(checked_at or '')} ({count} моделей).</span>"
            )
        else:
            base = (
                f'<span style="color:#c0392b">Не удалось обновить список моделей '
                f"в {html.escape(checked_at or '')} — показан прежний список "
                f"({count} моделей).</span>"
            )
        if errors:
            details = "<br>".join(
                f"{html.escape(name)}: {html.escape(str(text))}" for name, text in errors.items()
            )
            footer = f'<p style="color:#555">{base} Ошибки:<br>{details}</p>'
        else:
            footer = f'<p style="color:#555">{base}</p>'

    note_html = (
        f'<p style="color:#b8860b">{html.escape(snapshot["note"])}</p>' if snapshot["note"] else ""
    )

    # Баннер «проверка выполняется» + JS поллинга /api/refresh-state.
    # Вставляются только при running=True: без идущей проверки поллинга
    # нет (страница не перезагружается сама по себе). JS опрашивает
    # состояние каждые 2 с; увидев завершение (running=false, done_at
    # есть) — перезагружает страницу, чтобы показать свежий результат.
    banner_html = ""
    poll_script = ""
    if running:
        started = time.strftime("%H:%M:%S", time.localtime(started_at)) if started_at else ""
        banner_html = (
            '<p style="background:#fff8e1;border:1px solid #e0c060;'
            f'padding:8px 12px;color:#8a6d1a">⟳ Проверка выполняется'
            f"{(' (запущена в ' + started + ')') if started else ''}… "
            "модели и API-эндпойнты пере-проверяются в фоне, страница "
            "обновится автоматически по завершении.</p>"
        )
        poll_script = """
<script>
  function status_poll() {{
    fetch("/api/refresh-state")
      .then(function (r) {{ return r.json(); }})
      .then(function (s) {{
        if (!s.running && s.done_at) {{ location.reload(); }}
        else {{ setTimeout(status_poll, 2000); }}
      }})
      .catch(function () {{ setTimeout(status_poll, 2000); }});
  }}
  window.addEventListener("load", status_poll);
</script>
"""

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
<script>
  function models_toggle(btn) {{
    var span = btn.previousElementSibling;
    if (span && span.classList.contains("models-extra")) {{
      var expanded = span.style.display !== "none";
      span.style.display = expanded ? "none" : "block";
      btn.textContent = expanded
        ? "Показать ещё (" + (btn.dataset.modelsCount - {MODEL_LINES}) + ")"
        : "Свернуть";
    }}
  }}
</script>
{poll_script}</head>
<body>
<h2>[CC]-adapter — статус</h2>
<p><b>Версия кода:</b> {html.escape(context.version)} &nbsp;·&nbsp;
   <b>Режим:</b> {html.escape(snapshot["mode"])} &nbsp;·&nbsp;
   <a href="/session">просмотр сессий →</a> &nbsp;·&nbsp;
   <a href="/config">runtime config →</a></p>
{note_html}
{banner_html}
<table>
  <tr><th>Backend</th><th>Base URL</th><th>Status</th><th>Endpoints</th><th>Models</th></tr>
  {"".join(rows)}
</table>
<h3 style="margin-top:24px">Использованные модели</h3>
<p style="color:#888;margin:4px 0 0 0">Модели, к которым агент обращался
  после старта адаптера (после прохождения строгой проверки). Первое
  обращение к модели проверяет доступные API-эндпоинты короткими запросами
  именно этой моделью (до 4×5 с); повторные обращения не перепроверяются —
  растёт только счётчик «Вызовов». Таблица живёт в памяти процесса и
  сбрасывается при старте; ADAPTER_MODEL_USAGE_ENABLE=0 — учёт остаётся,
  пробы выключены (колонки эндпоинтов «—»).</p>
<table>
  <tr><th>Модель</th><th>Бэкенд</th><th>Вызовов</th><th>completions</th><th>messages</th><th>responses</th><th>embeddings</th></tr>
  {_usage_rows_html(model_usage.usage_snapshot())}
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
    """Эндпойнт "/": статус-страница (версия, эндпойнты LLM, API, модели).

    Проверка бэкендов — по кнопке «⟳ Проверить сейчас» (POST "/") и при
    автостарте (первый GET, см. _autostart_first_check). POST работает по
    PRG-паттерну: запускает ФОНОВУЮ проверку config.start_refresh(timeout=
    PROBE_TIMEOUT) и отвечает 303 See Other на GET "/" (HTTP не ждёт её
    завершения; браузер переходит на страницу GET-навигацией, поэтому
    авто-релоад не повторяет POST). Загрузка страницы (GET "/") показывает
    состояние последней проверки (config.refresh_state). Пока проверка идёт,
    страница показывает баннер и авто-обновляется по завершении (JS
    status_poll → /api/refresh-state → location.reload()). Периодического
    фонового refresh нет — только явный POST или автостарт."""

    prefix = "/"

    def __init__(self, context):
        self.context = context

    def _render_from_state(self):
        """Отрендерить страницу из текущего состояния проверки.

        Если настроенных эндпойнтов нет (standalone без env-бэкенда) —
        проверку запускать нечего: страница рендерится с подсказкой."""
        state = config.refresh_state()
        return _render_status_page(
            self.context,
            refresh=_last_result(state),
            checked_at=state.get("checked_at"),
            running=state.get("running"),
            started_at=state.get("started_at"),
        )

    def GET(self, handler, remainder: str):
        if remainder:
            handler.send_error(404, "Not found")
            return
        # Первый заход на страницу запускает первую проверку автоматически
        # (см. _autostart_first_check); _render_from_state после вызова
        # отрендерит баннер, если проверка реально стартовала.
        _autostart_first_check()
        handler._write(200, "text/html; charset=utf-8", self._render_from_state())

    def POST(self, handler, remainder: str):
        # Кнопка «⟳ Проверить сейчас»: PRG-паттерн — запускаем фоновую
        # проверку и отвечаем 303 See Other на GET "/", чтобы браузер
        # перешёл на неё GET-навигацией. Иначе авто-обновление страницы
        # (location.reload()) повторяло бы POST, а браузер спрашивал бы
        # «повторить действие?» (диалог Chrome/Firefox). Если проверка уже
        # идёт — start_refresh вернёт False; редирект всё равно уводит на
        # GET, который отрендерит баннер. В standalone без настроенных
        # эндпойнтов проверять нечего — не запускаем (GET покажет подсказку).
        if _collect_endpoints():
            config.start_refresh(timeout=PROBE_TIMEOUT)
        handler._redirect("/")


@webserver.register
class RefreshStateEndpoint(webserver.Endpoint):
    """Эндпойнт "/api/refresh-state": состояние фоновой проверки (JSON).

    Лёгкий ответ для JS status_poll на статус-странице: config.refresh_state()
    — {"running", "started_at", "done_at", "ok", "count", "errors",
    "checked_at"}. GET проверку не запускает и ничего не мутирует —
    безопасно опрашивать каждые 2 с."""

    prefix = "/api/refresh-state"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        body = json.dumps(config.refresh_state()).encode("utf-8")
        handler._write(200, "application/json; charset=utf-8", body)


def _last_result(state: dict) -> dict | None:
    """refresh-срез состояния для _render_status_page (или None).

    Проверок ещё не было (ok/count/errors пусты) → None: футер показывает
    подсказку, а не «обновлено 0 моделей». Иначе — {"ok", "count",
    "errors"} как раньше приходил из refresh_models."""
    if state.get("ok") is None and state.get("errors") is None:
        return None
    return {"ok": state.get("ok"), "count": state.get("count"), "errors": state.get("errors")}


def _autostart_first_check() -> bool:
    """Запустить фоновую проверку на первом GET "/", если проверок ещё не
    было и есть что проверять. Возвращает True, если запущена этим вызовом.

    Сценарии:
      - standalone (python -m backend_adapter.webserver) с YAML в
        ADAPTER_BACKEND_CONFIG: _BACKENDS пуст (адаптер не стартовал), но
        _collect_endpoints() вернёт список из YAML — первый GET запускает
        проверку, чтобы колонка Endpoints и модели заполнились без клика;
      - в процессе адаптера стартовую проверку уже запустил
        backend-adapter.py (running=True) — повторно не гоним;
      - проверка уже завершалась (done_at есть) — не гоним повторно:
        повторные проверки — только по кнопке «⟳ Проверить сейчас»."""
    if not _collect_endpoints():  # standalone без конфига — нечего проверять
        return False
    state = config.refresh_state()
    if state.get("running"):
        return False  # уже идёт (стартовая адаптера / по кнопке)
    if state.get("done_at") is not None:
        return False  # проверка уже завершалась — не гоним повторно
    return config.start_refresh(timeout=PROBE_TIMEOUT)


__all__ = [
    "PROBE_TIMEOUT",
    "MODEL_LINES",
    "_collect_endpoints",
    "_config_snapshot",
    "_models_html",
    "_api_html",
    "_usage_endpoint_html",
    "_usage_rows_html",
    "_render_status_page",
    "StatusEndpoint",
    "RefreshStateEndpoint",
    "_last_result",
    "_autostart_first_check",
]
