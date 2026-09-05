#!/usr/bin/env python3
"""
webui_status.py — эндпойнт "/" общего веб-сервера WEBUI: статус-страница.

Показывает на одной странице:
  - версию кода (из WebContext.version — в адаптере это __version__ из
    backend-adapter.py, единственный источник);
  - режим работы (multi-backend / standalone);
  - каждый настроенный LLM-эндпойнт: доступность, список моделей и
    колонку «Доступные API» — какие известные API-эндпойнты бэкенд реально
    обслуживает (результат дымовой пробы config.probe_endpoints, см. ниже).

Откуда данные:
  - GET "/" и POST "/" (кнопка «⟳ Проверить сейчас») делают ОДНО И ТО ЖЕ:
    вызывают config.refresh_models() — живую пере-пробу каждого эндпойнта
    с жёстким коротким таймаутом PROBE_TIMEOUT (5 с) и обновляют
    конфиг-глобалы адаптера (_AVAILABLE_MODELS/_MODEL_TO_BACKEND/
    _BACKENDS/_BACKEND_BY_NAME). Обновление происходит только по явному
    сигналу (старт адаптера, загрузка страницы, кнопка) — периодического
    фонового refresh НЕТ.
  - Страница рендерится из обновлённых глобалов: бэкенд мог добавить
    новые модели между стартами адаптера (или после ошибки 400 «model is
    not available»), refresh подхватывает их без перезапуска — следующие
    запросы /v1/messages с новыми моделями проходят строгую валидацию.
  - refresh_models возвращает {"ok", "count", "errors"}; при полном
    провале старый список моделей сохраняется и показывается на странице
    вместе с текстом ошибки; при частичном успехе страница показывает
    свежий список ответивших бэкендов и тексты ошибок упавших.
  - Дымовая проба API: refresh_models дополнительно несёт "probe" —
    результат config.probe_endpoints(): какие из известных эндпойнтов
    (/v1/chat/completions, /v1/messages, /v1/responses, /v1/embeddings)
    бэкенд обслуживает. Проба — короткие POST с max_tokens:1 (модель —
    из необязательного ключа probe YAML-записи бэкенда, либо первая из
    /v1/models), результат кэшируется ~60 с (ENDPOINT_PROBE_TTL) и
    отключается флагом ADAPTER_ENDPOINT_PROBE=0. Для каждого бэкенда
    колонка «Доступные API» показывает найденные/ненайденные пути
    (из config._ENDPOINT_STATE).

САМОСТОЯТЕЛЬНЫЙ ЗАПУСК (standalone — python -m backend_adapter.webserver
вне процесса адаптера): конфиг-глобалы адаптера пусты (нет YAML-конфига),
поэтому страница показывает режим standalone и подсказку, как получить
живые данные, — вместо фантомного списка «example.com» из дефолтов env.
Эндпойнты для refresh в этом режиме задаются той же переменной
окружения, что у адаптера, — ADAPTER_BACKEND_CONFIG (путь к YAML);
модуль берёт её из config так же, как сам адаптер.

Чистая логика (snapshot, refresh, рендер) вынесена в отдельные функции,
чтобы её можно было тестировать без HTTP-сервера.
"""

import html
import logging
import os
import time

from . import config, webserver

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
    ещё не пробован (standalone / ADAPTER_ENDPOINT_PROBE=0). Найденные
    эндпойнты — зелёным с ✓, ненайденные (404) — серым «—»; пропущенный
    из-за отсутствующей probe-модели эндпойнт в api не значится и
    показывается серым «—», остальные — по результатам. Порядок —
    config.ENDPOINT_PROBES (тот же, что у самой пробы)."""
    if api is None:
        return '<span style="color:#999">не опрошено</span>'
    parts = []
    for _pname, path, _tpl in config.ENDPOINT_PROBES:
        label = path[len("/v1/") :]
        ep = api.get(label)
        if ep is None:
            parts.append(f'<span style="color:#aaa" title="эндпоинт не пробован">{label} —</span>')
        elif ep["found"]:
            extra = ""
            if ep["status"] not in (None, 200):
                extra = f' <span style="color:#aaa;font-size:12px">({ep["status"]})</span>'
            parts.append(f'<span style="color:#1a7f37">{label} ✓</span>{extra}')
        else:
            parts.append(f'<span style="color:#aaa" title="HTTP {ep["status"]}">{label} —</span>')
    return "<br>".join(parts)


def _render_status_page(context, refresh=None, checked_at=None) -> bytes:
    """HTML статус-страницы.

    ``refresh`` — результат config.refresh_models(): {"ok", "count",
    "errors": {имя_бэкенда: текст}} (или None — когда обновлять было нечего:
    standalone без настроенных эндпойнтов). checked_at — время последнего
    обновления (или None).

    Статусы строк берутся из snapshot, но поверх: если бэкенд есть в
    refresh["errors"] — строка показывает «недоступен (текст ошибки)».
    Модели — из обновлённых конфиг-глобалов: при полном провале refresh
    кэш не тронут (показывается прежний список), при частичном — упавший
    бэкенд честно без моделей. Колонка «Доступные API» рендерится из
    config._ENDPOINT_STATE через _collect_endpoints (сама проба выполняется
    внутри refresh_models; refresh["probe"] отдельно не рендерится)."""
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
            '<p style="color:#888">Список моделей обновляется при каждой '
            "загрузке страницы (GET /v1/models, таймаут 5 с на эндпойнт); "
            "заодно проверяются API-эндпойнты бэкендов (POST max_tokens:1, "
            "кэш 60 с, ADAPTER_ENDPOINT_PROBE=0 — отключить).</p>"
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
</head>
<body>
<h2>[CC]-adapter — статус</h2>
<p><b>Версия кода:</b> {html.escape(context.version)} &nbsp;·&nbsp;
   <b>Режим:</b> {html.escape(snapshot["mode"])} &nbsp;·&nbsp;
   <a href="/session">просмотр сессий →</a> &nbsp;·&nbsp;
   <a href="/config">runtime config →</a></p>
{note_html}
<table>
  <tr><th>Backend</th><th>Base URL</th><th>Status</th><th>Endpoints</th><th>Models</th></tr>
  {"".join(rows)}
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

    GET и POST делают одно и то же: обновляют список моделей
    (config.refresh_models с коротким таймаутом PROBE_TIMEOUT; внутри —
    и дымовая проба API-эндпойнтов, результат — в колонке «Доступные
    API» из config._ENDPOINT_STATE) и рендерят страницу из обновлённых
    глобалов. Обновление только по явному сигналу (загрузка страницы /
    кнопка «⟳ Проверить сейчас») — никакого периодического refresh."""

    prefix = "/"

    def __init__(self, context):
        self.context = context

    def _refresh_and_render(self):
        """Пере-опросить эндпойнты и отрендерить страницу с результатом.

        Если настроенных эндпойнтов нет (standalone без env-бэкенда) —
        refresh_models вызывать нечего: страница рендерится с подсказкой."""
        if not _collect_endpoints():
            return _render_status_page(self.context)
        result = config.refresh_models(timeout=PROBE_TIMEOUT)
        return _render_status_page(
            self.context, refresh=result, checked_at=time.strftime("%H:%M:%S")
        )

    def GET(self, handler, remainder: str):
        if remainder:
            handler.send_error(404, "Not found")
            return
        handler._write(200, "text/html; charset=utf-8", self._refresh_and_render())

    def POST(self, handler, remainder: str):
        # Тело формы не читаем — кнопка одна, действий нет.
        handler._write(200, "text/html; charset=utf-8", self._refresh_and_render())


__all__ = [
    "PROBE_TIMEOUT",
    "MODEL_LINES",
    "_collect_endpoints",
    "_config_snapshot",
    "_models_html",
    "_api_html",
    "_render_status_page",
    "StatusEndpoint",
]
